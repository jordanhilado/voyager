#!/usr/bin/env bash
# macOS / Linux installer: venv under ~/.voyager, the `voyager` CLI, optional login agent.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
data="${VOYAGER_DATA_DIR:-$HOME/.voyager}"
mkdir -p "$data"
py="${PYTHON:-python3}"
"$py" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [ ! -x "$data/venv/bin/python" ]; then "$py" -m venv "$data/venv"; fi
"$data/venv/bin/pip" install -q --upgrade pip
"$data/venv/bin/pip" install -q -e "$here[dev]"
mkdir -p "$HOME/.local/bin"
ln -sf "$data/venv/bin/voyager" "$HOME/.local/bin/voyager"
ln -sf "$data/venv/bin/jevd" "$HOME/.local/bin/jevd"
echo "installed: $HOME/.local/bin/voyager (make sure ~/.local/bin is on your PATH)"

# Claude Code integration: the /voyager skill, the voyager MCP server (user scope), and permissions so
# a single prompt runs without per-tool prompts (cu_commit still asks).
mkdir -p "$HOME/.claude/skills"
ln -sfn "$here/skills/voyager" "$HOME/.claude/skills/voyager"
echo "skill installed: ~/.claude/skills/voyager -> $here/skills/voyager"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove voyager -s user >/dev/null 2>&1 || true
  claude mcp add --transport http --scope user voyager http://127.0.0.1:8765/mcp >/dev/null && echo "MCP server 'voyager' registered at user scope"
  "$data/venv/bin/python" "$here/scripts/claude_permissions.py" && echo "Claude Code permissions updated (~/.claude/settings.json, backup kept)"
else
  echo "claude CLI not found: skipping MCP registration (run: claude mcp add --transport http --scope user voyager http://127.0.0.1:8765/mcp)"
fi

if [ "${1:-}" = "--login" ] && [ "$(uname)" = "Darwin" ]; then
  plist="$HOME/Library/LaunchAgents/ai.voyager.jevd.plist"
  cat > "$plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.voyager.jevd</string>
  <key>ProgramArguments</key><array><string>$data/venv/bin/voyager</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$data/jevd.log</string>
  <key>StandardErrorPath</key><string>$data/jevd.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict></plist>
PL
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "login agent installed: $plist (logs: $data/jevd.log)"
  echo "put TYPESAFE_API_KEY (required) and ANTHROPIC_API_KEY / NTFY_TOPIC (optional) in $data/config.yaml for the agent to see them"
fi

echo
echo "next: voyager doctor"
echo "      then in any Claude Code session: /voyager go to flights.google.com and ..."
