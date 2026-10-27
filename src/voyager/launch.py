"""Open a URL in a desktop browser (macOS / Windows / Linux) without going through the model."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import IS_MAC, IS_WINDOWS

MAC_BROWSERS = ["Google Chrome", "Dia", "Safari", "Microsoft Edge", "Arc", "Brave Browser", "Firefox"]
_LOCAL = os.environ.get("LOCALAPPDATA", "")
WIN_BROWSERS = {
    "Google Chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                      os.path.join(_LOCAL, r"Google\Chrome\Application\chrome.exe")],
    "Microsoft Edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
    "Brave Browser": [r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                      os.path.join(_LOCAL, r"BraveSoftware\Brave-Browser\Application\brave.exe")],
    "Firefox": [r"C:\Program Files\Mozilla Firefox\firefox.exe", r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"],
}
WIN_EXES = {"Google Chrome": "chrome.exe", "Microsoft Edge": "msedge.exe", "Brave Browser": "brave.exe", "Firefox": "firefox.exe"}


def win_browser_exe(name: str) -> str | None:
    """The browser's executable: a known install path, else its App Paths registry entry."""
    found = next((p for p in WIN_BROWSERS.get(name, []) if p and os.path.exists(p)), None)
    if found or name not in WIN_EXES:
        return found
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{WIN_EXES[name]}") as k:
                path = str(winreg.QueryValue(k, None)).strip('"')
        except OSError:
            continue
        if os.path.exists(path):
            return path
    return None


def installed_browsers() -> list[str]:
    if IS_MAC:
        out = []
        for b in MAC_BROWSERS:
            if any((Path(d) / f"{b}.app").exists() for d in ("/Applications", str(Path.home() / "Applications"))):
                out.append(b)
        return out
    if IS_WINDOWS:
        return [name for name in WIN_BROWSERS if win_browser_exe(name)]
    return [b for b in ("google-chrome", "chromium", "firefox") if shutil.which(b)]


def pick_browser(preferred: str, allowed: list[str], running: list[str] | None = None) -> str:
    """The configured browser if installed; else an allowed browser that is already running;
    else the first installed browser that is allowed."""
    installed = installed_browsers()
    if preferred and preferred in installed:
        return preferred
    for b in running or []:
        if b in installed and (not allowed or b in allowed):
            return b
    for b in installed:
        if not allowed or b in allowed:
            return b
    return installed[0] if installed else ""


def open_url(url: str, browser: str) -> None:
    if IS_MAC:
        args = ["open", "-a", browser, url] if browser else ["open", url]
        subprocess.run(args, check=True, timeout=20)
        return
    if IS_WINDOWS:
        exe = win_browser_exe(browser) if browser else None
        if exe:
            subprocess.Popen([exe, url])
        else:
            os.startfile(url)  # type: ignore[attr-defined]
        return
    subprocess.Popen([browser or "xdg-open", url])
