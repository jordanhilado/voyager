"""macOS backend: native accessibility reads and actions, native window listing and capture.

Reads go through ``macax.AXReader`` (tens to a few hundred ms, no fixed wait). Actions use the
accessibility API first, so they work on a background window without moving the pointer:

- click: ``AXPress`` (buttons, links, rows), ``AXFocused`` for text inputs, ``AXShowMenu`` for a
  right click. A real pointer click (app raised, pointer restored afterwards) is used only for
  double clicks, Box coordinates, elements without a press action, and retries (``mouse=True``).
- set_value: ``AXValue``.
- paste / type / keys: key events posted to the app's process; paste restores the clipboard.
- scroll: pixel scroll-wheel events at the element or window center.

Window listing and screenshots come from ``macwin`` (Core Graphics, ScreenCaptureKit);
screenshots need Screen Recording for the app that starts the daemon, and without it observations
carry no image. Box coordinates in screenshot pixels are mapped to screen points through the
window frame.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import ApplicationServices as AS
import Quartz
from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString, NSRunningApplication

from ..config import Settings
from . import macwin
from .base import Backend, ElementError, Observation, RuntimeError_, TargetInfo
from .macax import TEXT_INPUT_AX, AXReader, Frame, Snapshot
from .macwin import find_app

log = logging.getLogger("voyager.backend.macnative")

ERR_INVALID = {AS.kAXErrorInvalidUIElement, AS.kAXErrorCannotComplete}

# US-layout virtual key codes.
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
    "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "esc": 53,
    "delete": 117, "home": 115, "end": 119, "page_up": 116, "prior": 116, "page_down": 121, "next": 121,
    "left": 123, "right": 124, "down": 125, "up": 126, "kp_enter": 76, "kp_return": 76,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111,
    "kp_0": 82, "kp_1": 83, "kp_2": 84, "kp_3": 85, "kp_4": 86, "kp_5": 87, "kp_6": 88, "kp_7": 89,
    "kp_8": 91, "kp_9": 92, "kp_decimal": 65, "kp_add": 69, "kp_subtract": 78, "kp_multiply": 67,
    "kp_divide": 75, "kp_equal": 81,
}
ALIASES = {"back_space": "backspace", "esc": "escape", "pageup": "page_up", "pagedown": "page_down", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/", "semicolon": ";", "apostrophe": "'", "bracketleft": "[", "bracketright": "]", "grave": "`", "backslash": "\\"}
MODIFIERS = {
    "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate, "opt": Quartz.kCGEventFlagMaskAlternate,
    "super": Quartz.kCGEventFlagMaskCommand, "cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
    "meta": Quartz.kCGEventFlagMaskCommand, "win": Quartz.kCGEventFlagMaskCommand,
}


def parse_chord(key: str) -> tuple[int, int]:
    """xdotool-style chord ('ctrl+a', 'Return', 'super+shift+Tab') -> (keycode, flags)."""
    parts = [p for p in key.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key")
    flags = 0
    for m in parts[:-1]:
        f = MODIFIERS.get(m.lower())
        if f is None:
            raise ValueError(f"unknown modifier {m!r} in {key!r}")
        flags |= f
    k = parts[-1]
    name = ALIASES.get(k.lower(), k.lower())
    if name not in KEYCODES:
        raise ValueError(f"unknown key {k!r}")
    if len(k) == 1 and k.isupper():
        flags |= Quartz.kCGEventFlagMaskShift
    return KEYCODES[name], flags


class MacNativeBackend(Backend):
    platform = "mac"
    settle_quiet_s = 0.3
    settle_poll_s = 0.05

    def __init__(self, settings: Settings):
        self.settings = settings
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="macax")
        self.reader: AXReader | None = None
        self.app_name = ""
        self.pid = 0
        self._snap: Snapshot | None = None
        self._bound: TargetInfo | None = None
        self._wid: tuple[Frame, int] | None = None  # (window frame, CG window number) of the last capture
        self._shot_warned = False
        self._shot: tuple[tuple[int, int], Frame] | None = None  # (screenshot size, window frame at capture)
        self.last_timing: dict[str, float] = {}

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    # ---- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        if not macwin.screen_recording_granted():
            log.warning("Screen Recording is not granted to the app that started the daemon; "
                        "observations will carry no screenshot (image grounding and needs_help images are off)")

    async def stop(self) -> None:
        self._exec.shutdown(wait=False)

    async def reset(self) -> None:
        self._snap = None
        self._wid = None

    # ---- targets ------------------------------------------------------------------------
    async def list_targets(self) -> list[TargetInfo]:
        return await self._run(macwin.list_targets)

    async def bind(self, target: TargetInfo) -> Observation:
        name = str(target.app or target.id)
        app = await self._run(find_app, name)
        if app is None:
            await asyncio.to_thread(subprocess.run, ["open", "-g", "-a", name], check=False, timeout=20)
            deadline = time.time() + 15
            while app is None and time.time() < deadline:
                await asyncio.sleep(0.3)
                app = await self._run(find_app, name)
        if app is None:
            raise RuntimeError_(f"could not find or launch the app {name!r}")
        self.pid = int(app.processIdentifier())
        self.app_name = str(app.localizedName() or name)
        self.reader = await self._run(lambda: AXReader(self.pid, self.app_name, margin=self.settings.native_margin))
        self._bound = target
        self._wid = None
        if target.kind == "window":
            await self._run(self._raise_window, target)
        deadline = time.time() + 15
        while True:
            try:
                return await self.observe_ax()
            except LookupError:
                if time.time() > deadline:
                    raise RuntimeError_(f"{self.app_name} has no window to bind")
                await asyncio.sleep(0.3)

    # ---- observation ----------------------------------------------------------------------
    def _need(self) -> AXReader:
        if self.reader is None:
            raise RuntimeError_("no bound target; call cu_bind first")
        return self.reader

    async def observe_ax(self, scope: str = "action") -> Observation:
        reader = self._need()
        snap: Snapshot = await self._run(reader.read, scope)
        self._snap = snap
        obs = Observation.from_text(snap.text, t_ms=snap.ms)
        for e in obs.tree.elements:
            e.bounds = snap.frames.get(e.index)
        obs.partial = snap.partial
        self.last_timing = {"obs_ms": snap.ms, "nodes": snap.nodes}
        return obs

    def _window_id(self) -> tuple[int, Frame]:
        """CG window number and frame of the bound window, matched by frame (no public API maps an
        AX window to its number). Cached while the window has not moved or resized."""
        reader = self._need()
        win = reader.window if reader.window is not None else reader.resolve_window()
        frame = reader.frame_of(win) if win is not None else None
        if frame is None:
            raise RuntimeError_(f"{self.app_name} has no window to capture")
        if self._wid and self._wid[0] == frame:
            return self._wid[1], frame
        wid = macwin.match_window(frame, macwin.cg_windows(self.pid))
        if wid is None:
            raise RuntimeError_(f"no on-screen window of {self.app_name} matches the bound window")
        self._wid = (frame, wid)
        return wid, frame

    def _capture(self) -> tuple[bytes, Frame]:
        wid, frame = self._window_id()
        try:
            return macwin.capture_window(wid), frame
        except macwin.CaptureError as e:
            raise RuntimeError_(f"screenshot failed: {e}") from e

    def _raise_window(self, target: TargetInfo) -> None:
        """Make the chosen window the app's focused one (the reader follows the focused window)."""
        wins = macwin.ax_windows(self.pid)
        cg = macwin.cg_windows(self.pid)
        pick = next((w for w, _, f in wins if isinstance(target.id, int) and macwin.match_window(f, cg) == target.id), None)
        if pick is None and target.title:
            pick = next((w for w, t, _ in wins if t == target.title), None)
        if pick is not None:
            AS.AXUIElementSetAttributeValue(pick, "AXMain", True)
            AS.AXUIElementPerformAction(pick, "AXRaise")

    async def screenshot(self) -> bytes:
        from ..images import load

        shot, frame = await self._run(self._capture)
        self._shot = (load(shot).size, frame)
        return shot

    async def observe_full(self) -> Observation:
        shot: bytes | None = None
        try:
            shot = await self.screenshot()
        except RuntimeError_ as e:
            # Callers treat a missing image as "text only"; say why once instead of every step.
            (log.debug if self._shot_warned else log.warning)("%s", e)
            self._shot_warned = True
        obs = await self.observe_ax()
        obs.screenshot = shot
        if shot and self._shot:
            obs.shot_size = self._shot[0]
        return obs

    # ---- helpers (worker thread) ------------------------------------------------------------
    def _ref(self, index: int) -> Any:
        if self._snap is None or index not in self._snap.refs:
            raise ElementError(f"{index} is an invalid element ID")
        return self._snap.refs[index]

    def _check(self, err: int, what: str) -> None:
        if err in ERR_INVALID:
            raise ElementError(f"{what}: invalid element ({err})")
        if err:
            raise RuntimeError_(f"{what} failed ({err})")

    def _role(self, ref: Any) -> str:
        err, v = AS.AXUIElementCopyAttributeValue(ref, "AXRole", None)
        if err in ERR_INVALID:
            raise ElementError(f"invalid element ({err})")
        return str(v) if not err and v else ""

    def _actions(self, ref: Any) -> list[str]:
        err, names = AS.AXUIElementCopyActionNames(ref, None)
        if err in ERR_INVALID:
            raise ElementError(f"invalid element ({err})")
        return [str(n) for n in names] if not err and names else []

    def _activate(self) -> None:
        """Bring the app forward. Text insertion and pointer clicks need it; macOS ignores
        ``activateWithOptions`` from a background process, so this goes through LaunchServices."""
        if not self._is_front():
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
            url = app.bundleURL() if app is not None else None
            subprocess.run(["open", "-a", str(url.path()) if url else self.app_name], check=False, timeout=10)
            deadline = time.time() + 2.0
            while not self._is_front() and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.05)
        win = self.reader.window if self.reader else None
        if win is not None:
            AS.AXUIElementPerformAction(win, "AXRaise")

    def _is_front(self) -> bool:
        """Live check through AX (NSRunningApplication.isActive is stale off the main thread)."""
        if self.reader is None:
            return False
        err, v = AS.AXUIElementCopyAttributeValue(self.reader.app, "AXFrontmost", None)
        return not err and bool(v)

    def _hit_ok(self, x: float, y: float) -> bool:
        """True when the bound app owns whatever is on screen at (x, y)."""
        err, el = AS.AXUIElementCopyElementAtPosition(AS.AXUIElementCreateSystemWide(), x, y, None)
        if err or el is None:
            return False
        err, pid = AS.AXUIElementGetPid(el, None)
        return not err and pid == self.pid

    def _center(self, ref: Any) -> tuple[float, float]:
        """On-screen center of ``ref``, scrolling it into view first when needed. Chromium clips
        the frame of an element scrolled out of its container to a sliver at the container's
        edge, so a thin frame counts as hidden too."""
        assert self.reader
        f = self.reader.frame_of(ref)
        wf = self.reader.frame_of(self.reader.window) if self.reader.window is not None else None

        def hidden(fr: Any) -> bool:
            return not fr or fr[2] <= 2 or fr[3] <= 2 or bool(wf and not (wf[1] <= fr[1] + fr[3] / 2 <= wf[1] + wf[3]))

        if hidden(f):
            AS.AXUIElementPerformAction(ref, "AXScrollToVisible")
            time.sleep(0.15)
            f = self.reader.frame_of(ref)
        if hidden(f):
            raise ElementError("element has no on-screen frame to click")
        return f[0] + f[2] / 2, f[1] + f[3] / 2

    def _hit_is(self, ref: Any, x: float, y: float) -> bool:
        """True when the element under (x, y) is ``ref`` or inside it: a pointer click lands on
        the grounded element and nothing else."""
        err, el = AS.AXUIElementCopyElementAtPosition(AS.AXUIElementCreateSystemWide(), x, y, None)
        for _ in range(12):
            if err or el is None:
                return False
            if el == ref:
                return True
            err, el = AS.AXUIElementCopyAttributeValue(el, "AXParent", None)
        return False

    def _mouse_click(self, x: float, y: float, count: int = 1, button: str = "left") -> None:
        self._activate()
        if not self._hit_ok(x, y):
            time.sleep(0.2)
            if not self._hit_ok(x, y):
                raise RuntimeError_(f"another window covers ({x:.0f},{y:.0f}); refusing a pointer click that would land outside {self.app_name}")
        saved = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        pt = Quartz.CGPointMake(x, y)
        if button == "right":
            down, up, btn = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight
        else:
            down, up, btn = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft
        move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, btn)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.03)
        for i in range(1, count + 1):
            for kind in (down, up):
                ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, btn)
                Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.02)
        time.sleep(0.05)
        Quartz.CGWarpMouseCursorPosition(saved)
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)

    def _post_key(self, keycode: int, flags: int = 0) -> None:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPostToPid(self.pid, ev)
            time.sleep(0.008)

    def _post_text(self, text: str) -> None:
        for line_no, line in enumerate(text.split("\n")):
            if line_no:
                # A newline types as Shift+Return: a plain Return sends in most chat composers, and
                # sending is a commit that only cu_commit may perform.
                self._post_key(KEYCODES["return"], Quartz.kCGEventFlagMaskShift)
            for i in range(0, len(line), 16):
                chunk = line[i : i + 16]
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
                    Quartz.CGEventPostToPid(self.pid, ev)
                time.sleep(0.01)

    def _focus(self, ref: Any) -> None:
        err = AS.AXUIElementSetAttributeValue(ref, "AXFocused", True)
        if err in ERR_INVALID:
            raise ElementError(f"focus: invalid element ({err})")

    def _xy_to_screen(self, xy: tuple[int, int]) -> tuple[float, float]:
        if not self._shot:
            raise RuntimeError_("coordinate click without a screenshot to map from")
        (sw, sh), (wx, wy, ww, wh) = self._shot
        return wx + xy[0] * ww / sw, wy + xy[1] * wh / sh

    # ---- actions (worker thread) ----------------------------------------------------------------
    def _do_click(self, index: int | None, xy: tuple[int, int] | None, count: int, button: str, mouse: bool) -> str:
        if index is None:
            x, y = self._xy_to_screen(xy)  # type: ignore[arg-type]
            self._mouse_click(x, y, count, button)
            return "mouse_xy"
        ref = self._ref(index)
        role = self._role(ref)
        if not mouse and count == 1:
            if button == "right":
                if AS.AXUIElementPerformAction(ref, "AXShowMenu") == 0:
                    return "ax_menu"
            elif role in TEXT_INPUT_AX:
                self._focus(ref)
                return "ax_focus"
            elif "AXPress" in self._actions(ref):
                # Only when advertised: some Chromium forks (Dia) accept AXPress and do nothing.
                err = AS.AXUIElementPerformAction(ref, "AXPress")
                if err == 0:
                    return "ax_press"
                if err in ERR_INVALID:
                    raise ElementError(f"press: invalid element ({err})")
        x, y = self._center(ref)
        self._activate()
        if not self._hit_is(ref, x, y):
            AS.AXUIElementPerformAction(ref, "AXScrollToVisible")
            time.sleep(0.15)
            x, y = self._center(ref)
            if not self._hit_is(ref, x, y):
                raise ElementError("something else is on top of the target at its on-screen position")
        self._mouse_click(x, y, count, button)
        return "mouse"

    def _do_set_value(self, index: int, value: str) -> None:
        ref = self._ref(index)
        err, settable = AS.AXUIElementIsAttributeSettable(ref, "AXValue", None)
        if err in ERR_INVALID:
            raise ElementError(f"set value: invalid element ({err})")
        if err or not settable:
            raise ElementError("Cannot set a value for an element that is not settable")
        self._check(AS.AXUIElementSetAttributeValue(ref, "AXValue", value), "set value")

    def _do_paste(self, text: str, index: int | None) -> None:
        self._activate()  # text insertion is ignored by a background app
        if index is not None:
            self._focus(self._ref(index))
            time.sleep(0.03)
        pb = NSPasteboard.generalPasteboard()
        saved: list[list[tuple[str, Any]]] = []
        for item in pb.pasteboardItems() or []:
            saved.append([(t, item.dataForType_(t)) for t in item.types()])
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        try:
            self._post_key(KEYCODES["v"], Quartz.kCGEventFlagMaskCommand)
            time.sleep(0.25)  # the app reads the pasteboard asynchronously
        finally:
            pb.clearContents()
            items = []
            for types in saved:
                it = NSPasteboardItem.alloc().init()
                for t, data in types:
                    if data is not None:
                        it.setData_forType_(data, t)
                items.append(it)
            if items:
                pb.writeObjects_(items)

    def _do_type(self, text: str, index: int | None) -> None:
        self._activate()
        if index is not None:
            self._focus(self._ref(index))
            time.sleep(0.03)
        self._post_text(text)

    def _do_key(self, key: str, index: int | None) -> None:
        try:
            code, flags = parse_chord(key)
        except ValueError as e:
            raise RuntimeError_(str(e)) from e
        if index is not None:
            self._focus(self._ref(index))
        self._post_key(code, flags)

    def _do_scroll(self, direction: str, amount: int, index: int | None, xy: tuple[int, int] | None) -> str:
        """Scroll by ``amount`` pages. Vertical scrolls of an element or the page use
        ``AXScrollToVisible`` on the item ~0.8 pages past the edge (background-safe); coordinates,
        horizontal scrolls and content without separate items fall back to the scroll wheel."""
        if xy is None and direction in ("up", "down"):
            how = "ax"
            for _ in range(amount):
                r = self._ax_page(direction, index)
                if r == "edge":
                    return "ax_edge" if how == "ax" else how
                if r == "none":
                    how = "wheel"
                    break
            else:
                return how
        self._wheel_scroll(direction, amount, index, xy)
        return "wheel"

    def _ax_page(self, direction: str, index: int | None) -> str:
        """One page via AXScrollToVisible. Returns "ok", "edge" (nothing further) or "none"."""
        assert self.reader
        snap = self.reader.read("action")
        from ..ax import parse_ax

        tree = parse_ax(snap.text)
        if index is not None:
            ref = self._ref(index)
            root = next((e for e in tree.elements if snap.refs.get(e.index) == ref), None)
        else:  # the page: web content if any (browser toolbars follow it in tree order), else the window
            root = next((e for e in tree.elements if e.role in ("HTML content", "web area")), None) or (tree.elements[0] if tree.elements else None)
        if root is None or root.index not in snap.frames:
            return "none"
        vf = snap.frames[root.index]
        if snap.window_frame:  # the visible part of the root
            wf = snap.window_frame
            top, bottom = max(vf[1], wf[1]), min(vf[1] + vf[3], wf[1] + wf[3])
            vf = (vf[0], top, vf[2], max(bottom - top, 0))
        leaves = [e for e in tree.subtree(root) if not e.children and e.index != root.index and e.index in snap.frames]
        vis = [e for e in leaves if snap.frames[e.index][3] > 1 and snap.frames[e.index][1] >= vf[1] - 1
               and snap.frames[e.index][1] + snap.frames[e.index][3] <= vf[1] + vf[3] + 1]
        if not vis:
            return "none"
        pos = {e.index: i for i, e in enumerate(leaves)}
        k = max(1, int(len(vis) * 0.8))
        if direction == "down":
            rest = leaves[pos[vis[-1].index] + 1 :]
            if not rest:
                return "edge"
            target = rest[min(k, len(rest)) - 1]
        else:
            rest = leaves[: pos[vis[0].index]]
            if not rest:
                return "edge"
            target = rest[-min(k, len(rest))]
        self._snap = snap
        err = AS.AXUIElementPerformAction(snap.refs[target.index], "AXScrollToVisible")
        if err:
            return "none"
        time.sleep(0.05)
        return "ok"

    def _wheel_scroll(self, direction: str, amount: int, index: int | None, xy: tuple[int, int] | None) -> None:
        """Real scroll-wheel events; they go to the window under the pointer, so the app is raised,
        the pointer parked over the target and restored afterwards."""
        assert self.reader
        wf = self.reader.frame_of(self.reader.window) if self.reader.window is not None else None
        if index is not None:
            f = self.reader.frame_of(self._ref(index))
            if f and wf:  # clip to the visible part of the window
                top, bottom = max(f[1], wf[1]), min(f[1] + f[3], wf[1] + wf[3])
                f = (f[0], top, f[2], max(bottom - top, 1))
        elif xy is not None:
            x, y = self._xy_to_screen(xy)
            f = (x - 1, y - 1, 2, 2)
        else:
            f = wf
        if not f:
            raise RuntimeError_("nothing to scroll")
        x, y = f[0] + f[2] / 2, f[1] + f[3] / 2
        span = f[3] if direction in ("up", "down") else f[2]
        page = max(min(span if span > 2 else 600, (wf[3] if wf else 900)) * 0.8, 100)
        total = int(page * amount)
        sign = {"down": -1, "up": 1, "right": -1, "left": 1}[direction]
        vertical = direction in ("up", "down")
        self._activate()
        if not self._hit_ok(x, y):
            raise RuntimeError_(f"another window covers ({x:.0f},{y:.0f}); refusing to scroll outside {self.app_name}")
        saved = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(x, y))
        try:
            done = 0
            while done < total:
                d = min(120, total - done)
                ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2, sign * d if vertical else 0, 0 if vertical else sign * d)
                Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(x, y))
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                done += d
                time.sleep(0.012)
            time.sleep(0.05)
        finally:
            Quartz.CGWarpMouseCursorPosition(saved)
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)

    # ---- Backend API ------------------------------------------------------------------------------
    async def _after(self, t0: float, how: str = "") -> Observation:
        act_ms = (time.perf_counter() - t0) * 1000
        await asyncio.sleep(self.settings.native_action_delay_s)
        obs = await self.observe_ax()
        self.last_timing = {**self.last_timing, "act_ms": round(act_ms, 1), "how": how}
        return obs

    async def click(self, index=None, xy=None, *, count=1, button="left", mouse=False) -> Observation:
        t0 = time.perf_counter()
        how = await self._run(self._do_click, index, xy, count, button, mouse)
        return await self._after(t0, how)

    async def set_value(self, index: int, value: str) -> Observation:
        t0 = time.perf_counter()
        await self._run(self._do_set_value, index, value)
        return await self._after(t0, "ax_value")

    async def paste(self, text: str, index: int | None = None) -> Observation:
        t0 = time.perf_counter()
        await self._run(self._do_paste, text, index)
        return await self._after(t0, "paste")

    async def type_text(self, text: str, index: int | None = None) -> Observation:
        t0 = time.perf_counter()
        await self._run(self._do_type, text, index)
        return await self._after(t0, "keys")

    async def press_key(self, key: str, index: int | None = None) -> Observation:
        t0 = time.perf_counter()
        await self._run(self._do_key, key, index)
        return await self._after(t0, "key")

    async def scroll(self, direction: str, amount: int = 1, index=None, xy=None) -> Observation:
        t0 = time.perf_counter()
        how = await self._run(self._do_scroll, direction, amount, index, xy)
        return await self._after(t0, f"scroll_{how}")


def native_available() -> tuple[bool, str]:
    """Whether the native backend can run here, and why not."""
    try:
        import ApplicationServices as _AS  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"pyobjc is not installed ({e})"
    if not AS.AXIsProcessTrusted():
        return False, "this process is not trusted for Accessibility (System Settings > Privacy & Security > Accessibility)"
    return True, ""
