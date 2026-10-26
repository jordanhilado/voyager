"""Settings: environment variables, an optional YAML file, and platform defaults.

Precedence: environment variable > ``~/.voyager/config.yaml`` > default.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def default_data_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "voyager"
    return Path.home() / ".voyager"


@dataclass
class Settings:
    # Daemon
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = field(default_factory=default_data_dir)
    log_level: str = "INFO"

    # Apps the daemon may bind when the brief does not list its own.
    allowed_apps: list[str] = field(default_factory=lambda: ["Google Chrome", "Dia", "Safari", "Microsoft Edge"])
    # Native reads cover the window plus this many window-heights above and below it.
    native_margin: float = 1.0
    # Pause between a native action and its first read, so the app can process the event.
    native_action_delay_s: float = 0.05
    # Browser used by cu_open ("" = pick the first installed of the usual ones)
    browser: str = ""

    # Eyes: Jev (TypeSafe) is required; Claude (Anthropic) answers only when a Jev call fails
    typesafe_api_key: str = ""
    typesafe_api: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-1.14.0"
    anthropic_api_key: str = ""
    eyes_model: str = "claude-haiku-5-5"
    planner_model: str = "claude-sonnet-5-5"

    # Notifications
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Budgets
    max_steps: int = 80
    max_minutes: int = 45
    max_consecutive_failures: int = 3
    settle_cap_s: float = 2.5
    run_log_days: int = 14

    # Misc
    briefs_dir: Path = field(default_factory=lambda: Path.cwd() / "briefs")
    hotkey: bool = True

    @property
    def log_path(self) -> Path:
        return self.data_dir / "jevd.log"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "driver.lock"

    @property
    def thresholds_path(self) -> Path:
        return self.data_dir / "thresholds.yaml"


_ENV_MAP = {
    "host": "VOYAGER_HOST",
    "port": "VOYAGER_PORT",
    "data_dir": "VOYAGER_DATA_DIR",
    "log_level": "VOYAGER_LOG_LEVEL",
    "native_margin": "VOYAGER_NATIVE_MARGIN",
    "allowed_apps": "VOYAGER_ALLOWED_APPS",
    "browser": "VOYAGER_BROWSER",
    "typesafe_api_key": "TYPESAFE_API_KEY",
    "typesafe_api": "TYPESAFE_API_URL",
    "jev_model": "VOYAGER_JEV_MODEL",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "eyes_model": "VOYAGER_EYES_MODEL",
    "planner_model": "VOYAGER_PLANNER_MODEL",
    "ntfy_topic": "NTFY_TOPIC",
    "ntfy_server": "NTFY_SERVER",
    "max_steps": "VOYAGER_MAX_STEPS",
    "max_minutes": "VOYAGER_MAX_MINUTES",
    "briefs_dir": "VOYAGER_BRIEFS_DIR",
    "hotkey": "VOYAGER_HOTKEY",
}


def _coerce(name: str, value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, Path):
        return Path(str(value)).expanduser()
    if isinstance(current, list) and isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_settings(config_path: Path | None = None) -> Settings:
    s = Settings()
    path = config_path or Path(os.environ.get("VOYAGER_CONFIG", default_data_dir() / "config.yaml"))
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        for f in fields(s):
            if f.name in data:
                setattr(s, f.name, _coerce(f.name, data[f.name], getattr(s, f.name)))
    for f in fields(s):
        env = _ENV_MAP.get(f.name)
        if env and os.environ.get(env) not in (None, ""):
            setattr(s, f.name, _coerce(f.name, os.environ[env], getattr(s, f.name)))
    s.data_dir = Path(s.data_dir).expanduser()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
