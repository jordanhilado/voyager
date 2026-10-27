# Windows installer: venv under %LOCALAPPDATA%\voyager, the `voyager` command, the /voyager skill,
# the MCP server for Claude Code, and optionally a logon task that keeps the daemon running.
#
#   powershell -NoProfile -File install.ps1           # install
#   powershell -NoProfile -File install.ps1 -Login    # also start the daemon at every logon (and now)
param(
    [switch]$Login,
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = if ($env:VOYAGER_DATA_DIR) { $env:VOYAGER_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "voyager" }
New-Item -ItemType Directory -Force $data | Out-Null

# Python 3.11+: -Python, then the py launcher, then python on PATH (the Store alias does not count).
function Test-Python($cmd, [string[]]$pre) {
    try { & $cmd @pre -c "import sys; assert sys.version_info >= (3, 11)" 2>$null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
$py = $null; $pyArgs = @()
if ($Python) { $py = $Python }
elseif ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-3"))) { $py = "py"; $pyArgs = @("-3") }
else {
    foreach ($c in @("python", "python3")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*" -and (Test-Python $c @())) { $py = $c; break }
    }
}
if (-not $py) {
    throw "Python 3.11+ not found. Install it (winget install Python.Python.3.12) or pass -Python <path to python.exe>."
}

$venv = Join-Path $data "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) { & $py @pyArgs -m venv $venv; if ($LASTEXITCODE) { throw "venv creation failed" } }
& $vpy -m pip install -q --upgrade pip
& $vpy -m pip install -q -e "$here[dev,hotkey]"
if ($LASTEXITCODE) { throw "pip install failed" }

# `voyager` on PATH in ~/.local/bin (added to the user PATH if missing). pip's launcher .exe names the
# venv's python by absolute path, so a copy works from cmd, PowerShell and Git Bash (Claude Code's shell).
$bin = Join-Path $HOME ".local\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
foreach ($name in @("voyager", "jevd")) {
    Copy-Item -Force (Join-Path $venv "Scripts\$name.exe") (Join-Path $bin "$name.exe")
}
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($userPath -split ";") -contains $bin)) {
    [Environment]::SetEnvironmentVariable("Path", ($(if ($userPath) { "$userPath;" } else { "" }) + $bin), "User")
    Write-Host "added $bin to your user PATH (open a new terminal to pick it up)"
}
Write-Host "installed: $bin\voyager.exe (runs $venv)"

# Claude Code integration: the /voyager skill (a directory junction, no admin needed), the voyager MCP
# server (user scope) and permissions so a single prompt runs without per-tool prompts (cu_commit still asks).
$skills = Join-Path $HOME ".claude\skills"
New-Item -ItemType Directory -Force $skills | Out-Null
$skill = Join-Path $skills "voyager"
if (Test-Path $skill) {
    $item = Get-Item $skill -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $item.Delete() } else { throw "$skill exists and is not a link; move it away and re-run" }
}
New-Item -ItemType Junction -Path $skill -Target (Join-Path $here "skills\voyager") | Out-Null
Write-Host "skill installed: $skill -> $here\skills\voyager"
if (Get-Command claude -ErrorAction SilentlyContinue) {
    # Windows PowerShell turns a native command's stderr into a terminating error under "Stop"; "not
    # registered yet" on a first install is expected.
    $ErrorActionPreference = "Continue"
    & claude mcp remove voyager -s user 2>&1 | Out-Null
    & claude mcp add --transport http --scope user voyager http://127.0.0.1:8765/mcp 2>&1 | Out-Null
    $added = $LASTEXITCODE -eq 0
    $ErrorActionPreference = "Stop"
    if ($added) { Write-Host "MCP server 'voyager' registered at user scope" } else { Write-Host "MCP registration failed; run: claude mcp add --transport http --scope user voyager http://127.0.0.1:8765/mcp" }
    & $vpy (Join-Path $here "scripts\claude_permissions.py")
} else {
    Write-Host "claude CLI not found: skipping MCP registration (run: claude mcp add --transport http --scope user voyager http://127.0.0.1:8765/mcp)"
}

if ($Login) {
    # A per-user logon task runs the daemon in your desktop session without a console window.
    $task = "Voyager daemon"
    $action = New-ScheduledTaskAction -Execute (Join-Path $venv "Scripts\pythonw.exe") -Argument "-m voyager.cli serve" -WorkingDirectory $data
    # The token's account name (MACHINE\user or DOMAIN\user); $env:USERDOMAIN can read WORKGROUP.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $task
    Write-Host "logon task installed: '$task' (logs: $data\jevd.log); remove with: Unregister-ScheduledTask -TaskName '$task'"
    Write-Host "put TYPESAFE_API_KEY (required) and ANTHROPIC_API_KEY / NTFY_TOPIC (optional) in $data\config.yaml for the task to see them"
}

Write-Host ""
Write-Host "next: voyager doctor"
Write-Host "      then in any Claude Code session: /voyager go to flights.google.com and ..."
