#!/bin/bash
# On the VPS, as agent: put master live once the tests pass on it.
# tidyrack-follow.timer runs it every 2 minutes; logs: journalctl --user -u tidyrack-follow
# The commit that came up is written to ~/.local/state/tidyrack/live, and one that didn't (or failed its tests)
# to .../failed, so a bad commit is tried once and rolled back, not retried every 2 minutes.
set -euo pipefail

main() {
  export PATH="$HOME/.local/bin:$PATH"  # uv; a timer doesn't read the profile that adds it
  state=~/.local/state/tidyrack
  mkdir -p "$state"
  cd ~/code/wardrobe
  [ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "Changes in ~/code/wardrobe; not deploying over them" >&2; exit 1; }
  [ -f "$state/live" ] || git rev-parse HEAD > "$state/live"
  live=$(cat "$state/live")
  git fetch -q origin master
  want=$(git rev-parse origin/master)
  [ "$want" = "$live" ] && exit 0
  [ "$want" = "$(cat "$state/failed" 2>/dev/null)" ] && exit 0
  # The tests run here, not by asking GitHub: the VPS's GitHub token can't read Actions results.
  git checkout -q -B master "$want"
  if ! { uv sync -q --frozen && uv run --frozen pytest -q -x >/dev/null; }; then
    echo "Tests failed on ${want:0:7}; staying on ${live:0:7}"
    echo "$want" > "$state/failed"
    git checkout -q -B master "$live" && uv sync -q --frozen
    exit 0
  fi
  if put_live "$want"; then
    echo "$want" > "$state/live"
    echo "${want:0:7} is live"
    exit 0
  fi
  echo "$want" > "$state/failed"
  echo "${want:0:7} did not come up; going back to ${live:0:7}" >&2
  put_live "$live" || echo "${live:0:7} did not come up either; see journalctl --user -u tidyrack" >&2
  exit 1
}

# Check out a commit, install, restart, and wait until the page answers.
put_live() {
  git checkout -q -B master "$1" && uv sync -q --frozen && systemctl --user restart tidyrack || return 1
  for _ in $(seq 60); do
    curl -fs -m 2 -o /dev/null http://127.0.0.1:8760/ && return 0
    sleep 0.5
  done
  return 1
}

# Bash has read the whole file before main runs, so checking out a new version of it is safe.
main "$@"
