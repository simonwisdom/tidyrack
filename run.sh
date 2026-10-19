#!/usr/bin/env bash
# Start Tidyrack.
#   ./run.sh           serve it in this terminal at http://127.0.0.1:8760
#   ./run.sh install   run it as a background service that starts at login and restarts if it crashes
#   ./run.sh restart   restart the service, to pick up code changes
#   ./run.sh stop      stop the service and stop it starting at login
#   ./run.sh phone     share it over HTTPS to your own devices with Tailscale (`./run.sh phone off` stops sharing)
# The service gets the TIDYRACK_* and OPENROUTER_API_KEY settings of the shell you run `install` from;
# run `install` again after changing them.
set -euo pipefail
cd "$(dirname "$0")"
LABEL=app.tidyrack
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Tailscale's HTTPS port for the phone (TIDYRACK_TAILSCALE_PORT; the Phone button's QR code reads the same setting).
PHONE_PORT=${TIDYRACK_TAILSCALE_PORT:-10000}

unload() {
  launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
  # bootout returns before the job is gone, and bootstrap fails while it's still there.
  for _ in $(seq 40); do launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1 || break; sleep 0.25; done
}

wait_up() {
  for _ in $(seq 60); do
    curl -fs -o /dev/null http://127.0.0.1:8760/ && { echo "Tidyrack: running at http://127.0.0.1:8760"; return; }
    sleep 0.5
  done
  echo "Tidyrack did not come up; see ~/Library/Logs/tidyrack.log" >&2
  exit 1
}

case "${1:-}" in
  install)
    uv sync -q
    # A login service starts with almost no PATH: give it the folders codex and node are in
    # (codex starts with #!/usr/bin/env node, and nvm or mise keep node outside the usual folders).
    path="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    for tool in node codex; do
      found=$(command -v "$tool" || true)
      [ -n "$found" ] && path="$(dirname "$found"):$path"
    done
    sed -e "s|__UV__|$(command -v uv)|g" -e "s|__HOME__|$HOME|g" -e "s|__DIR__|$PWD|g" "launchd/$LABEL.plist.in" > "$PLIST"
    chmod 600 "$PLIST"  # it can hold an OpenRouter key
    plutil -replace EnvironmentVariables.PATH -string "$path" "$PLIST"
    # plutil escapes each value, so a key or path with odd characters can't break the file.
    for name in $(compgen -e | grep -E '^(TIDYRACK_|OPENROUTER_API_KEY$)' || true); do
      plutil -replace "EnvironmentVariables.$name" -string "${!name}" "$PLIST"
    done
    plutil -lint "$PLIST" >/dev/null
    unload
    launchctl bootstrap "gui/$UID" "$PLIST"
    wait_up
    ;;
  restart)
    launchctl kickstart -k "gui/$UID/$LABEL"
    wait_up
    ;;
  stop)
    unload
    rm -f "$PLIST"
    echo "Tidyrack: stopped"
    ;;
  phone)
    command -v tailscale >/dev/null || { echo "Install Tailscale first: https://tailscale.com/download" >&2; exit 1; }
    if [ "${2:-}" = off ]; then
      tailscale serve --https=$PHONE_PORT off
      echo "Tidyrack: no longer shared"
      exit
    fi
    # The server itself stays on 127.0.0.1; Tailscale gives an address only your own devices can open.
    tailscale serve --bg --https=$PHONE_PORT http://127.0.0.1:8760
    host=$(tailscale status --json | uv run python -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
    echo "Tidyrack: open https://$host:$PHONE_PORT on your iPhone (with Tailscale on)"
    ;;
  deploy)
    # For a server that follows master (deploy/): test, push master, wait until it's live.
    VPS=${TIDYRACK_VPS:?set TIDYRACK_VPS to the SSH name of the server that runs deploy/follow.sh}
    # Only what's merged and committed goes live.
    [ "$(git branch --show-current)" = master ] || { echo "Deploy from master: merge your branch first" >&2; exit 1; }
    [ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "Commit your changes first" >&2; exit 1; }
    uv run pytest -q
    git push -q origin master
    want=$(git rev-parse HEAD)
    # GitHub runs the tests again; once they pass, Railway and the VPS (deploy/follow.sh) each pull master.
    # deploy/follow.sh writes down the commit that came up, and one that didn't.
    echo "Tidyrack: pushed ${want:0:7}; waiting for the VPS to put it live (up to 10 minutes)"
    for _ in $(seq 60); do
      read -r live failed <<<"$(ssh "$VPS" 'cd ~/.local/state/tidyrack && echo "$(cat live) $(cat failed 2>/dev/null)"')"
      [ "$live" = "$want" ] && { echo "Tidyrack: ${live:0:7} is live on the VPS; Railway follows on its own"; exit 0; }
      [ "${failed:-}" = "$want" ] && break
      sleep 10
    done
    echo "The VPS didn't put ${want:0:7} live; it runs ${live:0:7}. See: ssh $VPS journalctl --user -u tidyrack-follow -n 20" >&2
    exit 1
    ;;
  "")
    echo "Tidyrack: open http://127.0.0.1:8760"
    exec uv run uvicorn tidyrack.app:app --host 127.0.0.1 --port 8760
    ;;
  *)
    echo "usage: ./run.sh [install|restart|stop|phone [off]]" >&2
    exit 2
    ;;
esac
