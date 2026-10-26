"""Global kill-switch hotkey (Ctrl+Alt+Shift+Q). Optional: needs ``pip install voyager-cu[hotkey]``
and, on macOS, Accessibility/Input Monitoring permission for the terminal running the daemon."""
from __future__ import annotations

import logging

from .policy import KillSwitch

log = logging.getLogger("voyager.hotkey")


class _Noop:
    def stop(self) -> None:
        return None


def start_hotkey(kill: KillSwitch, combo: str = "<ctrl>+<alt>+<shift>+q"):
    try:
        from pynput import keyboard
    except Exception:  # noqa: BLE001
        log.info("pynput not installed; global kill-switch hotkey disabled (pip install voyager-cu[hotkey])")
        return _Noop()
    try:
        listener = keyboard.GlobalHotKeys({combo: lambda: (log.warning("kill switch hotkey pressed"), kill.trip())})
        listener.daemon = True
        listener.start()
        log.info("kill switch hotkey armed: %s", combo)
        return listener
    except Exception as e:  # noqa: BLE001
        log.warning("hotkey unavailable: %s", e)
        return _Noop()
