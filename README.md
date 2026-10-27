# Voyager

Fast, safe computer use on your Mac or Windows PC. Claude plans a whole batch of steps at once, a small local daemon carries them out through the operating system's accessibility layer and checks each one, and safety rules in code pause before anything is sent.

```
Claude Code (planner)  →  voyager daemon (127.0.0.1:8765)  →  your app window
                              ├─ eyes: Jev (TypeSafe) verifies each step
                              └─ backend: macOS accessibility + ScreenCaptureKit
                                          Windows UI Automation + PrintWindow
```

## Setup

You need Python 3.11+ and [Claude Code](https://claude.com/claude-code) on macOS or Windows 10/11.

1. **Install**

   macOS:

   ```bash
   git clone https://github.com/jordanhilado/voyager && cd voyager
   ./install.sh
   ```

   Windows (PowerShell):

   ```powershell
   git clone https://github.com/jordanhilado/voyager; cd voyager
   powershell -NoProfile -File install.ps1 -Login
   ```

   This installs the `voyager` command, the `/voyager` skill and the MCP server for Claude Code. On Windows,
   `-Login` adds a logon task that keeps the daemon running in your desktop session (recommended; leave it
   out to start the daemon yourself with `voyager up`). If script execution is disabled, add
   `-ExecutionPolicy Bypass` before `-File`. No Python yet? `winget install Python.Python.3.12`.

2. **Add your keys** to the config file, `~/.voyager/config.yaml` on macOS or `%LOCALAPPDATA%\voyager\config.yaml`
   on Windows (see [Configuration](#configuration)):

   ```yaml
   typesafe_api_key: <your key>     # required
   anthropic_api_key: <your key>    # optional, recommended
   ```

3. **Permissions**
   - macOS: grant your terminal (or whichever app runs Claude Code) **Accessibility** (required) and
     **Screen & System Audio Recording** (optional; without it Voyager can't take screenshots) in System
     Settings > Privacy & Security, then restart it.
   - Windows: none. The daemon must run in your desktop session, not as a service: start it from a normal
     terminal or with the logon task. Voyager cannot drive apps running as administrator unless it runs
     elevated too.

4. **Check it:** `voyager doctor`

## Usage

In any Claude Code session:

> /voyager go to flights.google.com and find the cheapest one-way fare for every day in December from SFO to LAX

Voyager opens the site, does the work and reports back. It asks for your approval before it sends or submits anything.

## Configuration

Set these as environment variables or in the config file (lowercase, without the `VOYAGER_` prefix, e.g. `allowed_apps: Google Chrome, Calculator`).

| Variable | Required? | Purpose |
|---|---|---|
| `TYPESAFE_API_KEY` | **Required** | Jev, which checks every step. |
| `ANTHROPIC_API_KEY` | Optional | Backup if a Jev call fails (Claude answers just that call). Also needed for `voyager run`. |
| `VOYAGER_ALLOWED_APPS` | Optional | Apps Voyager may control, comma-separated. Default: Chrome, Dia, Safari, Edge. |
| `NTFY_TOPIC` | Optional | [ntfy](https://ntfy.sh) push when a send needs approval or a run stops. |
| `VOYAGER_PORT` | Optional | Daemon port. Default `8765` (localhost only). |

## Safety

- **You approve every send.** Send, submit and confirm steps always stop for your OK.
- **Consent, terms, credit checks and offers pause the run**, whatever the task says.
- **Hard limits in code:** app and site allowlists, never types into password fields, refuses card numbers, SSNs and one-time codes.
- **Stop anytime** with `voyager stop`.

Screen text from the controlled window goes to TypeSafe (and to Anthropic only if a Jev call fails). Run logs stay local in `runs/` under the data directory (`~/.voyager` on macOS, `%LOCALAPPDATA%\voyager` on Windows).

## Commands

| Command | What it does |
|---|---|
| `voyager doctor` | Check permissions, keys and setup |
| `voyager up` / `down` | Start or stop the daemon (the skill starts it for you) |
| `voyager stop` | Abort the current run |
| `voyager tail -f` | Watch a run live |
| `voyager run "<goal>"` | Run without Claude Code (needs `ANTHROPIC_API_KEY`) |

## Windows notes

- Reads and actions go through UI Automation patterns (Invoke, Toggle, Value, Scroll), so most steps work on a
  background window without moving the pointer. Typing, keys and pointer clicks bring the window to the
  foreground first, because Windows delivers synthesized input only there; the pointer goes back where it was.
- Key chords use xdotool names; `cmd+…` means Ctrl, so plans written with macOS shortcuts behave the same.
  `super`/`win` is the Windows key.
- Screenshots use `PrintWindow`, which works for covered windows but not minimized ones.
- `python scripts/e2e_windows.py --local` runs an offline smoke test against a small test window
  (`scripts/win_fixture.py`); `python scripts/e2e_calculator.py` drives Windows Calculator.

## Development

```bash
pip install -e ".[dev]" && pytest -q
```

Benchmark against Cua Driver (macOS): [`bench/`](bench/README.md) (results in [`bench/RESULTS.md`](bench/RESULTS.md)).

## License

[MIT](LICENSE)
