"""Native Windows window listing, app lookup and launch, foreground and capture (pywin32 + ctypes).

- Listing: visible, unowned, uncloaked top-level windows with a title (what Alt+Tab shows). The app
  name is the executable's FileDescription ("Microsoft Edge", "Google Chrome", "Notepad"), so the
  names in ``allowed_apps`` read the same as on macOS. Store apps run inside an
  ``ApplicationFrameHost`` frame; their name comes from the hosted ``CoreWindow``'s process.
  Installed apps that are not running are listed from the shell's Apps folder, so a bind can
  launch them.
- Foreground: input (keys, text, pointer) goes to the foreground window, so actions that need it
  bring the bound window forward first. Windows only lets the process that received the last input
  take the foreground; the daemon attaches to the foreground thread's input queue for the switch.
- Capture: ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` captures one window even when it is
  covered (it does not work for minimized windows), cropped to the DWM frame without the
  invisible resize borders. A black frame (some GPU-composited windows) falls back to a screen
  grab of the window's area when the window is on top there.

Coordinates are physical pixels: the daemon sets per-monitor DPI awareness before any call.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process

from .base import TargetInfo

log = logging.getLogger("voyager.backend.winwin")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
FRAME_HOSTS = {"applicationframehost.exe"}

Frame = tuple[float, float, float, float]


class CaptureError(Exception):
    """The window could not be captured (minimized, gone, capture failed)."""


def set_dpi_aware() -> None:
    """Per-monitor DPI awareness: UIA frames, window rects, the cursor and screenshots all use
    physical pixels. Must run before the process reads any coordinates."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def interactive_session() -> bool:
    """False when the daemon runs where there is no desktop to drive (a service, session 0)."""
    sid = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
        return True
    return sid.value != 0


# ---- processes ------------------------------------------------------------------------------------
def exe_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


_desc_cache: dict[str, str] = {}


def file_description(path: str) -> str:
    """The executable's FileDescription ("Microsoft Edge"), else its file name without extension."""
    if not path:
        return ""
    if path in _desc_cache:
        return _desc_cache[path]
    desc = ""
    try:
        lang, cp = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        desc = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription") or ""
    except Exception:  # noqa: BLE001  (no version resource, access denied)
        desc = ""
    desc = desc.strip() or Path(path).stem
    _desc_cache[path] = desc
    return desc


# ---- windows ----------------------------------------------------------------------------------------
def _cloaked(hwnd: int) -> bool:
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return bool(v.value)


def window_frame(hwnd: int) -> Frame | None:
    """The visible frame (DWM extended frame bounds, without the invisible resize borders)."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        try:
            l, t, rr, b = win32gui.GetWindowRect(hwnd)
        except win32gui.error:
            return None
        r.left, r.top, r.right, r.bottom = l, t, rr, b
    w, h = r.right - r.left, r.bottom - r.top
    return (float(r.left), float(r.top), float(w), float(h)) if w > 0 and h > 0 else None


def window_pid(hwnd: int) -> int:
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except win32gui.error:
        return 0


def hosted_pid(hwnd: int) -> int:
    """For a Store app frame, the pid of the app inside it (its ``CoreWindow``); else the window's pid."""
    pid = window_pid(hwnd)
    if os.path.basename(exe_path(pid)).lower() not in FRAME_HOSTS:
        return pid
    found: list[int] = []

    def cb(child, _):
        if win32gui.GetClassName(child) == "Windows.UI.Core.CoreWindow":
            p = window_pid(child)
            if p and p != pid:
                found.append(p)
                return False
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except win32gui.error:
        pass  # EnumChildWindows reports an error when the callback stops it early
    return found[0] if found else pid


def app_name_of(hwnd: int) -> str:
    pid = window_pid(hwnd)
    path = exe_path(pid)
    if os.path.basename(path).lower() in FRAME_HOSTS:
        inner = hosted_pid(hwnd)
        if inner != pid:
            return file_description(exe_path(inner))
        return win32gui.GetWindowText(hwnd)  # a suspended Store app: its frame title is its name
    return file_description(path)


def top_windows() -> list[int]:
    """Alt+Tab windows in z-order (topmost first)."""
    out: list[int] = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or not win32gui.GetWindowText(h):
            return True
        if win32gui.GetWindow(h, win32con.GW_OWNER):
            return True
        ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW and not ex & win32con.WS_EX_APPWINDOW:
            return True
        if _cloaked(h):
            return True
        out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return out


def popups_of(hwnd: int) -> list[int]:
    """Visible windows owned (directly or through other owned windows) by ``hwnd``, plus visible
    popup menus of its process: dialogs, menus, dropdowns. Bottom-most first."""
    pid = window_pid(hwnd)
    out: list[int] = []

    def owned_by(h: int) -> bool:
        seen = 0
        o = win32gui.GetWindow(h, win32con.GW_OWNER)
        while o and seen < 8:
            if o == hwnd:
                return True
            o = win32gui.GetWindow(o, win32con.GW_OWNER)
            seen += 1
        return False

    def cb(h, _):
        if h == hwnd or not win32gui.IsWindowVisible(h):
            return True
        if owned_by(h) or (win32gui.GetClassName(h) == "#32768" and window_pid(h) == pid):
            if window_frame(h) is not None:
                out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    out.reverse()
    return out


def resolve_window(current: int | None, pids: set[int]) -> int | None:
    """The window to read for an app: the foreground window when it belongs to the app (a new
    window the app or the user brought forward), else ``current`` while it is still shown, else the
    app's topmost window."""
    fg = foreground()
    if fg and fg != current and _is_app_window(fg, pids):
        return fg
    if current and win32gui.IsWindow(current) and win32gui.IsWindowVisible(current) and not _cloaked(current):
        return current
    for h in top_windows():
        if _is_app_window(h, pids):
            return h
    return None


def _is_app_window(h: int, pids: set[int]) -> bool:
    if win32gui.GetWindow(h, win32con.GW_OWNER) or not win32gui.IsWindowVisible(h):
        return False
    p = window_pid(h)
    return p in pids or hosted_pid(h) in pids


# ---- apps --------------------------------------------------------------------------------------------
_apps_cache: tuple[float, dict[str, str]] | None = None


def installed_apps() -> dict[str, str]:
    """Start-menu apps (desktop and Store) as {display name: shell parsing name}. Cached 5 minutes."""
    global _apps_cache
    if _apps_cache and time.time() - _apps_cache[0] < 300:
        return _apps_cache[1]
    apps: dict[str, str] = {}
    try:
        import win32com.client

        folder = win32com.client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
        for item in folder.Items():
            name, path = str(item.Name or ""), str(item.Path or "")
            if name and path and name not in apps:
                apps[name] = path
    except Exception as e:  # noqa: BLE001
        log.debug("Apps folder unavailable: %s", e)
    _apps_cache = (time.time(), apps)
    return apps


def find_window(name: str) -> int | None:
    """The topmost window of the app with this name (display name or executable name), or the window
    with this exact title."""
    low = name.lower().removesuffix(".exe")
    by_title = None
    for h in top_windows():
        app = app_name_of(h).lower()
        stem = Path(exe_path(hosted_pid(h))).stem.lower()
        if low in (app, stem):
            return h
        if by_title is None and win32gui.GetWindowText(h).lower() == low:
            by_title = h
    return by_title


def app_running(name: str) -> bool:
    """Whether a process of this app runs, with or without a window (a browser kept alive in the
    background must not be launched a second time)."""
    low = name.lower().removesuffix(".exe")
    try:
        pids = win32process.EnumProcesses()
    except Exception:  # noqa: BLE001
        return False
    for pid in pids:
        path = exe_path(pid)
        if path and low in (Path(path).stem.lower(), file_description(path).lower()):
            return True
    return False


def launch(name: str) -> bool:
    """Start an app by display name (Start menu), executable path, or executable name on PATH."""
    if os.path.isfile(name):
        subprocess.Popen([name], close_fds=True)
        return True
    apps = installed_apps()
    low = name.lower()
    key = next((k for k in apps if k.lower() == low), None) or next((k for k in apps if k.lower().startswith(low)), None)
    if key:
        os.startfile(f"shell:AppsFolder\\{apps[key]}")  # type: ignore[attr-defined]
        return True
    try:
        os.startfile(name)  # type: ignore[attr-defined]  # App Paths: "msedge", "notepad", ...
        return True
    except OSError:
        return False


def list_targets() -> list[TargetInfo]:
    """Windows of running apps, then installed apps that are not running."""
    out: list[TargetInfo] = []
    running: set[str] = set()
    for h in top_windows():
        app = app_name_of(h)
        running.add(app.lower())
        out.append(TargetInfo("window", int(h), title=win32gui.GetWindowText(h), app=app, running=True))
    for name in sorted(installed_apps()):
        if name.lower() not in running:
            out.append(TargetInfo("app", name, app=name, running=False))
    return out


# ---- foreground --------------------------------------------------------------------------------------
def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def is_foreground(hwnd: int) -> bool:
    fg = foreground()
    return bool(fg) and (fg == hwnd or (user32.GetAncestor(fg, 3) or 0) == hwnd)  # GA_ROOTOWNER


def activate(hwnd: int, timeout_s: float = 2.0) -> bool:
    """Bring ``hwnd`` to the foreground. A background process may not take the foreground directly,
    so the call is made with this thread attached to the foreground thread's input queue."""
    if is_foreground(hwnd):
        return True
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    fg = foreground()
    me = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(other and other != me and user32.AttachThreadInput(me, other, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, other, False)
    if not is_foreground(hwnd):
        # Windows grants the switch to the process that sent the last input: a zero-distance pointer
        # move is input that changes nothing (an Alt tap would open menu bars).
        user32.mouse_event(0x0001, 0, 0, 0, 0)  # MOUSEEVENTF_MOVE
        user32.SetForegroundWindow(hwnd)
    deadline = time.time() + timeout_s
    while not is_foreground(hwnd) and time.time() < deadline:
        time.sleep(0.02)
        user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return is_foreground(hwnd)


def root_at(x: float, y: float) -> int:
    """The top-level window under a screen point."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return (user32.GetAncestor(h, 2) or 0) if h else 0  # GA_ROOT


# ---- capture ----------------------------------------------------------------------------------------
def capture_window(hwnd: int, quality: int = 85) -> tuple[bytes, Frame]:
    """JPEG bytes of one window at physical pixel resolution, and the frame they cover."""
    import io

    import win32ui
    from PIL import Image

    if not win32gui.IsWindow(hwnd):
        raise CaptureError("the window is gone")
    if win32gui.IsIconic(hwnd):
        raise CaptureError("the window is minimized")
    frame = window_frame(hwnd)
    if frame is None:
        raise CaptureError("the window has no size")
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        ok = user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_RENDERFULLCONTENT)
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    fx, fy, fw, fh = (int(v) for v in frame)
    img = img.crop((fx - l, fy - t, fx - l + fw, fy - t + fh))
    if not ok or img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        img = _grab_screen(hwnd, frame)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), frame


def _grab_screen(hwnd: int, frame: Frame):
    from PIL import ImageGrab

    x, y, w, h = frame
    if root_at(x + w / 2, y + h / 2) != hwnd:
        raise CaptureError("PrintWindow returned an empty image and the window is covered")
    return ImageGrab.grab(bbox=(int(x), int(y), int(x + w), int(y + h)), all_screens=True).convert("RGB")
