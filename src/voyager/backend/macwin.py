"""Native macOS window listing and capture (pyobjc).

- Listing: regular running apps, their windows from the accessibility API (titles need no Screen
  Recording permission), paired with Core Graphics window numbers by frame. Installed apps that are
  not running are listed too, so a bind can launch them.
  ``NSWorkspace.runningApplications`` only updates on the main run loop, which the daemon does not
  run, so it goes stale; running apps are found through live lookups instead (window owners and
  ``NSRunningApplication`` by pid or bundle id).
- Capture: ScreenCaptureKit captures one window, even when it is covered or in the background,
  at the display's pixel scale (2x on Retina). ``screencapture -l`` is the fallback. On some
  setups (seen on macOS 26.6) ScreenCaptureKit lists windows but its capture call never returns;
  after the first timeout the daemon uses ``screencapture`` for the rest of its life.
  Both need Screen Recording for the app macOS holds responsible for the daemon (usually the
  terminal); without it ``capture_window`` raises ``CaptureError``.

All calls are synchronous; the backend runs them on its worker thread.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import ApplicationServices as AS
import Quartz
from AppKit import NSApplicationActivationPolicyRegular, NSBundle, NSRunningApplication, NSWorkspace
from Foundation import NSMutableData

from .base import TargetInfo
from .macax import Frame, _point, _size

log = logging.getLogger("voyager.backend.macwin")

APP_DIRS = [Path("/Applications"), Path("/Applications/Utilities"), Path("/System/Applications"),
            Path("/System/Applications/Utilities"), Path.home() / "Applications"]


class CaptureError(Exception):
    """The window could not be captured (no permission, window gone, capture failed)."""


def screen_recording_granted() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


# ---- Core Graphics windows ---------------------------------------------------------------------
def cg_windows(pid: int | None = None, on_screen_only: bool = False) -> list[dict]:
    """Normal-layer windows as ``{"id", "pid", "frame", "on_screen"}``."""
    opts = Quartz.kCGWindowListExcludeDesktopElements
    opts |= Quartz.kCGWindowListOptionOnScreenOnly if on_screen_only else Quartz.kCGWindowListOptionAll
    out: list[dict] = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if int(w.get(Quartz.kCGWindowLayer, 1)) != 0:
            continue
        owner = int(w.get(Quartz.kCGWindowOwnerPID, 0))
        if pid is not None and owner != pid:
            continue
        b = w.get(Quartz.kCGWindowBounds) or {}
        frame = (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
        if frame[2] < 2 or frame[3] < 2:
            continue
        out.append({"id": int(w[Quartz.kCGWindowNumber]), "pid": owner, "frame": frame,
                    "on_screen": bool(w.get(Quartz.kCGWindowIsOnscreen, False))})
    return out


def match_window(frame: Frame | None, windows: list[dict], tol: float = 4.0) -> int | None:
    """The window number whose bounds match an accessibility frame (both are global points with a
    top-left origin). Prefers on-screen windows when several match."""
    if frame is None:
        return None
    best: tuple[float, int] | None = None
    for w in windows:
        d = max(abs(a - b) for a, b in zip(frame, w["frame"]))
        if d > tol:
            continue
        key = d - (1.0 if w["on_screen"] else 0.0)
        if best is None or key < best[0]:
            best = (key, w["id"])
    return best[1] if best else None


# ---- accessibility windows ----------------------------------------------------------------------
def _attr(ref: Any, name: str) -> Any:
    err, v = AS.AXUIElementCopyAttributeValue(ref, name, None)
    return None if err else v


def ax_frame(ref: Any) -> Frame | None:
    err, vals = AS.AXUIElementCopyMultipleAttributeValues(ref, ["AXPosition", "AXSize"], 0, None)
    if err or not vals or len(vals) < 2:
        return None
    p, s = _point(vals[0]), _size(vals[1])
    return (p[0], p[1], s[0], s[1]) if p and s else None


def ax_windows(pid: int, timeout_s: float = 0.5) -> list[tuple[Any, str, Frame | None]]:
    """(element, title, frame) for each standard window of ``pid``."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, timeout_s)
    out = []
    for w in _attr(app, "AXWindows") or []:
        if (_attr(w, "AXSubrole") or "AXStandardWindow") not in ("AXStandardWindow", "AXDialog"):
            continue
        out.append((w, str(_attr(w, "AXTitle") or ""), ax_frame(w)))
    return out


# ---- targets ----------------------------------------------------------------------------------------
def installed_apps() -> list[str]:
    names: set[str] = set()
    for d in APP_DIRS:
        try:
            names.update(p.stem for p in d.iterdir() if p.suffix == ".app")
        except OSError:
            continue
    return sorted(names)


def running_apps() -> list[NSRunningApplication]:
    """Regular apps that are running now: every window owner plus the (possibly stale) workspace
    list, each re-checked by pid (that lookup is live and returns None once the app quits)."""
    pids = {w["pid"] for w in cg_windows()}
    pids |= {int(a.processIdentifier()) for a in NSWorkspace.sharedWorkspace().runningApplications()}
    out = []
    for pid in sorted(pids):
        a = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if a is not None and a.activationPolicy() == NSApplicationActivationPolicyRegular:
            out.append(a)
    return out


def find_app(name: str) -> NSRunningApplication | None:
    """The running app with this display name or bundle id, looked up live."""
    ws = NSWorkspace.sharedWorkspace()
    bids = [name] if "." in name else []
    path = ws.fullPathForApplication_(name)
    bundle = NSBundle.bundleWithPath_(path) if path else None
    if bundle is not None and bundle.bundleIdentifier():
        bids.insert(0, str(bundle.bundleIdentifier()))
    for bid in bids:
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)
        if apps is not None and len(apps):
            return apps[0]
    low = name.lower()
    for a in running_apps():
        if (a.localizedName() or "").lower() == low or (a.bundleIdentifier() or "").lower() == low:
            return a
    return None


def list_targets() -> list[TargetInfo]:
    """Windows of running apps, running apps without windows, then installed apps."""
    out: list[TargetInfo] = []
    running: set[str] = set()
    cg = cg_windows()
    for a in running_apps():
        name = str(a.localizedName() or a.bundleIdentifier() or "")
        if not name:
            continue
        pid = int(a.processIdentifier())
        running.add(name)
        mine = [w for w in cg if w["pid"] == pid]
        wins = ax_windows(pid)
        for i, (_, title, frame) in enumerate(wins):
            wid = match_window(frame, mine)
            out.append(TargetInfo("window", wid if wid is not None else f"{pid}:{i}", title=title, app=name, running=True))
        if not wins:
            out.append(TargetInfo("app", name, app=name, running=True))
    for name in installed_apps():
        if name not in running:
            out.append(TargetInfo("app", name, app=name, running=False))
    return out


# ---- capture ----------------------------------------------------------------------------------------
def _wait(start, timeout_s: float) -> tuple:
    """Run a ScreenCaptureKit call that reports through a completion handler and wait for it."""
    done = threading.Event()
    box: list = []

    def handler(*args):
        box.extend(args)
        done.set()

    start(handler)
    if not done.wait(timeout_s):
        raise CaptureError("ScreenCaptureKit timed out")
    return tuple(box)


def _jpeg(image: Any, quality: float = 0.85) -> bytes:
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    if dest is None:
        raise CaptureError("could not create a JPEG encoder")
    Quartz.CGImageDestinationAddImage(dest, image, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    if not Quartz.CGImageDestinationFinalize(dest):
        raise CaptureError("JPEG encoding failed")
    return bytes(data)


def _capture_sck(window_id: int, timeout_s: float) -> bytes:
    import ScreenCaptureKit as SCK

    content, err = _wait(lambda h: SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, False, h), timeout_s)
    if err is not None or content is None:
        raise CaptureError(f"window list unavailable: {err}")
    win = next((w for w in content.windows() if int(w.windowID()) == window_id), None)
    if win is None:
        raise CaptureError(f"window {window_id} is not capturable")
    filt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(win)
    rect, scale = filt.contentRect(), float(filt.pointPixelScale() or 1.0)
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setWidth_(max(1, round(rect.size.width * scale)))
    cfg.setHeight_(max(1, round(rect.size.height * scale)))
    cfg.setShowsCursor_(False)
    cfg.setIgnoreShadowsSingleWindow_(True)
    image, err = _wait(lambda h: SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(filt, cfg, h), timeout_s)
    if err is not None or image is None:
        raise CaptureError(f"capture failed: {err}")
    return _jpeg(image)


def _capture_cli(window_id: int, timeout_s: float) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="voyager-shot-")
    os.close(fd)
    try:
        r = subprocess.run(["screencapture", "-x", "-o", "-t", "jpg", f"-l{window_id}", path],
                           capture_output=True, timeout=timeout_s, check=False)
        data = Path(path).read_bytes()
        if r.returncode or not data:
            raise CaptureError(f"screencapture failed ({r.returncode}): {r.stderr.decode(errors='replace').strip()}")
        return data
    finally:
        Path(path).unlink(missing_ok=True)


# ScreenCaptureKit normally answers in 100-350 ms; a capture that has not returned by then is hung.
SCK_TIMEOUT_S = 1.5
_sck_hung = False  # set after the first timeout: ScreenCaptureKit is skipped from then on


def capture_window(window_id: int, timeout_s: float = 5.0) -> bytes:
    """JPEG bytes of one window at pixel resolution."""
    global _sck_hung
    if not screen_recording_granted():
        raise CaptureError("Screen Recording is not granted to the app that started the daemon "
                           "(System Settings > Privacy & Security > Screen & System Audio Recording)")
    sck_err: Exception | str = "skipped after an earlier timeout"
    if not _sck_hung:
        try:
            return _capture_sck(window_id, min(timeout_s, SCK_TIMEOUT_S))
        except CaptureError as e:
            sck_err = e
            if "timed out" in str(e):
                _sck_hung = True
                log.warning("ScreenCaptureKit capture timed out; using screencapture from now on")
        except Exception as e:  # noqa: BLE001  (pyobjc bridge errors, older macOS)
            sck_err = e
    try:
        return _capture_cli(window_id, timeout_s)
    except CaptureError as e:
        raise CaptureError(f"{e} (ScreenCaptureKit: {sck_err})") from e
