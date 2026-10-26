# Voyager

Fast, safe computer use on your Mac. Claude plans a whole batch of steps at once, a small local daemon carries them out through macOS accessibility and checks each one, and safety rules in code pause before anything is sent.

```
Claude Code (planner)  →  voyager daemon (127.0.0.1:8765)  →  your app window
                              ├─ eyes: Jev (TypeSafe) verifies each step
                              └─ backend: macOS accessibility + ScreenCaptureKit
```

macOS only for now. Windows is not implemented yet.

## Setup

You need macOS, Python 3.11+ and [Claude Code](https://claude.com/claude-code).

1. **Install**

   ```bash
   git clone https://github.com/jordanhilado/voyager && cd voyager
   ./install.sh
   ```

   This installs the `voyager` command, the `/voyager` skill and the MCP server for Claude Code.

2. **Add your keys** to `~/.voyager/config.yaml` (see [Configuration](#configuration)):

   ```yaml
   typesafe_api_key: <your key>     # required
   anthropic_api_key: <your key>    # optional, recommended
   ```

3. **Grant permissions** to your terminal (or whichever app runs Claude Code) in System Settings > Privacy & Security, then restart it:
   - **Accessibility**: required.
   - **Screen & System Audio Recording**: optional; without it Voyager can't take screenshots.

4. **Check it:** `voyager doctor`

## Usage

In any Claude Code session:

> /voyager go to flights.google.com and find the cheapest one-way fare for every day in December from SFO to LAX

Voyager opens the site, does the work and reports back. It asks for your approval before it sends or submits anything.

## Configuration

Set these as environment variables or in `~/.voyager/config.yaml` (lowercase, without the `VOYAGER_` prefix, e.g. `allowed_apps: Google Chrome, Calculator`).

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

Screen text from the controlled window goes to TypeSafe (and to Anthropic only if a Jev call fails). Run logs stay local in `~/.voyager/runs/`.

## Commands

| Command | What it does |
|---|---|
| `voyager doctor` | Check permissions, keys and setup |
| `voyager up` / `down` | Start or stop the daemon (the skill starts it for you) |
| `voyager stop` | Abort the current run |
| `voyager tail -f` | Watch a run live |
| `voyager run "<goal>"` | Run without Claude Code (needs `ANTHROPIC_API_KEY`) |

## Development

```bash
pip install -e ".[dev]" && pytest -q
```

Benchmark against Cua Driver: [`bench/`](bench/README.md) (results in [`bench/RESULTS.md`](bench/RESULTS.md)).

## License

[MIT](LICENSE)
