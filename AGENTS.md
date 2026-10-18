# Setting up Tidyrack for someone

You're a coding agent helping a person run Tidyrack on their own computer and open it from their phone. `GUIDE.md` explains every feature; this file is the setup path. Don't change their shell profiles or install anything system-wide without asking.

1. **Check the basics.** `uv --version` (install from https://docs.astral.sh/uv/ if missing) and that this is a Mac; on Linux everything works except `./run.sh install`, so run it with `./run.sh` or their own service manager.
2. **Install and test.** `uv sync`, then `uv run pytest -q`. The tests use a fake model: they're offline and free. Stop and report if they fail.
3. **Check Codex.** Tidyrack reads details, tidies pictures and draws outfits through the `codex` CLI on the person's own ChatGPT login. Run `codex --version` and `codex login status`. Drawing needs a paid ChatGPT plan.
   - If Codex isn't there and they don't want it, details can run on OpenRouter instead: they set `TIDYRACK_DETAILS=openrouter` and `OPENROUTER_API_KEY` (a paid key they make at openrouter.ai). Tidy-ups and drawings won't work without Codex; say so plainly.
   - Never ask them to paste a key into the chat; have them put it in the environment themselves.
4. **Start it.** `./run.sh install` (a login service on macOS, logs in `~/Library/Logs/tidyrack.log`) or `./run.sh` in a terminal. Check http://127.0.0.1:8760 answers. The first photo downloads the cutout model (about 180 MB).
5. **Reach it from the phone.** Ask which they want:
   - **Anywhere** (recommended): Tailscale on the computer and the phone, signed in to the same account, then `./run.sh phone`. It needs the `tailscale` command on the PATH (the Mac app installs it from its settings) and HTTPS turned on for the tailnet in Tailscale's admin console. It prints the address; the **Phone** button in the app shows a QR code.
   - **Home Wi-Fi only**: `./run.sh stop` if the service is running, then `uv run uvicorn tidyrack.app:app --host 0.0.0.0 --port 8760`, and open `http://<computer's local address>:8760` on the phone. Tell them anyone on that network can then open it, and that over plain http Save photos downloads instead of opening the share sheet.
6. **Tell them where their data is**: `data/` in this folder (or `TIDYRACK_DATA`). **Export** in the app downloads everything as a zip.

Don't set `TIDYRACK_PUBLIC`: that's for running a copy other people sign in to (GUIDE.md, "Run a public copy").

## Changing the code

- Python 3.11–3.13, FastAPI, SQLite; the front end is plain JavaScript in `static/`, no build step.
- Only `tidyrack/store.py` touches storage. Model calls live in `tidyrack/ai.py`.
- Run `uv run pytest -q` after each change.
