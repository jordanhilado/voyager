"""Windows backend: UI Automation reads and actions, native window listing and capture.

Reads go through ``winuia.UIAReader`` (one cached cross-process call per read). Actions use UI
Automation patterns first, so they work on a background window without moving the pointer:

- click: ``Invoke`` (buttons, links, menu items), ``Toggle`` (check boxes), ``SelectionItem``
  (list items, tabs, radio buttons in XAML/WPF apps only; see ``SELECT_FRAMEWORKS``),
  ``ExpandCollapse`` (combo boxes, menus), ``SetFocus`` for text inputs. A real pointer click (window brought forward, pointer restored afterwards) is used only
  for right and double clicks, Box coordinates, elements without a pattern, and retries
  (``mouse=True``).
- set_value: ``Value.SetValue``.
- paste / type / keys: ``SendInput`` to the foreground window (the bound window is brought forward
  first: Windows delivers synthesized input only to the foreground); paste restores the clipboard.
- scroll: ``Scroll`` on the nearest scrollable element (background-safe), ``ScrollItem`` paging,
  else wheel events at the element or window center.

Key chords use xdotool names. ``cmd``/``command`` mean Ctrl here, so a plan written with macOS
shortcuts (``cmd+a``) does the same thing; ``super``/``win``/``meta`` is the Windows key.

Window listing and screenshots come from ``winwin``. Coordinates are physical pixels throughout.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from typing import Any

import comtypes

from ..config import Settings
from . import winwin
from .base import Backend, ElementError, Observation, RuntimeError_, TargetInfo
from .winuia import INVALID_HRESULTS, UIA_E_ELEMENTNOTAVAILABLE, U, UIAReader, Snapshot, init_thread

log = logging.getLogger("voyager.backend.winnative")

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Frameworks whose SelectionItem.Select raises the app's own selection events. Win32 controls and
# Chromium change the selection without the notification the app listens for (LBN_SELCHANGE, a
# click handler), so a "click" there is a real click.
SELECT_FRAMEWORKS = {"XAML", "WinUI", "DirectUI", "WPF"}

# ---- keys ----------------------------------------------------------------------------------------
VK: dict[str, int] = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08, "escape": 0x1B,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "page_up": 0x21, "prior": 0x21,
    "page_down": 0x22, "next": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "kp_enter": 0x0D, "kp_return": 0x0D,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{f"kp_{i}": 0x60 + i for i in range(10)},
    "kp_decimal": 0x6E, "kp_add": 0x6B, "kp_subtract": 0x6D, "kp_multiply": 0x6A, "kp_divide": 0x6F,
    "kp_equal": 0xBB,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
    "]": 0xDD, "'": 0xDE,
}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x6F}
ALIASES = {"back_space": "backspace", "esc": "escape", "pageup": "page_up", "pagedown": "page_down", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/", "semicolon": ";", "apostrophe": "'", "bracketleft": "[", "bracketright": "]", "grave": "`", "backslash": "\\", "del": "delete", "ins": "insert"}
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B
MODIFIERS = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL, "cmd": VK_CONTROL, "command": VK_CONTROL,
    "shift": VK_SHIFT, "alt": VK_MENU, "option": VK_MENU, "opt": VK_MENU,
    "super": VK_LWIN, "win": VK_LWIN, "meta": VK_LWIN,
}


def parse_chord(key: str) -> tuple[int, list[int]]:
    """xdotool-style chord ('ctrl+a', 'Return', 'super+shift+Tab') -> (virtual key, modifier keys)."""
    parts = [p for p in key.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key")
    mods: list[int] = []
    for m in parts[:-1]:
        vk = MODIFIERS.get(m.lower())
        if vk is None:
            raise ValueError(f"unknown modifier {m!r} in {key!r}")
        if vk not in mods:
            mods.append(vk)
    k = parts[-1]
    name = ALIASES.get(k.lower(), k.lower())
    if name not in VK:
        raise ValueError(f"unknown key {k!r}")
    if len(k) == 1 and k.isupper() and VK_SHIFT not in mods:
        mods.append(VK_SHIFT)
    return VK[name], mods


# ---- SendInput ------------------------------------------------------------------------------------
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x800, 0x1000
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise RuntimeError_(f"SendInput was blocked (error {ctypes.get_last_error()}); is another app elevated or the desktop locked?")


def _key_input(vk: int, up: bool) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)))


def _unicode_input(unit: int, up: bool) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)))


def _mouse_input(flags: int, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, 0)))


def send_chord(vk: int, mods: list[int]) -> None:
    _send(*[_key_input(m, False) for m in mods], _key_input(vk, False), _key_input(vk, True),
          *[_key_input(m, True) for m in reversed(mods)])


def send_text(text: str) -> None:
    for line_no, line in enumerate(text.split("\n")):
        if line_no:
            # A newline types as Shift+Enter: a plain Enter sends in most chat composers, and
            # sending is a commit that only cu_commit may perform.
            send_chord(VK["return"], [VK_SHIFT])
        units = line.encode("utf-16-le")
        codes = [int.from_bytes(units[i : i + 2], "little") for i in range(0, len(units), 2)]
        for i in range(0, len(codes), 16):
            _send(*[inp for c in codes[i : i + 16] for inp in (_unicode_input(c, False), _unicode_input(c, True))])
            time.sleep(0.01)


# ---- clipboard ------------------------------------------------------------------------------------
def _clipboard_swap(text: str | None, saved: dict[int, Any] | None = None) -> dict[int, Any]:
    """Put ``text`` on the clipboard (or restore ``saved`` when text is None); returns what was there."""
    import win32clipboard as cb

    for _ in range(20):  # another app may hold the clipboard for a moment
        try:
            cb.OpenClipboard(None)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.03)
    else:
        raise RuntimeError_("the clipboard is busy")
    try:
        old: dict[int, Any] = {}
        if text is not None:
            fmt = 0
            while True:
                fmt = cb.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                try:
                    data = cb.GetClipboardData(fmt)
                except Exception:  # noqa: BLE001  (formats pywin32 cannot read)
                    continue
                # Handle formats (CF_BITMAP, ...) come back as ints and die with EmptyClipboard; the same
                # image is kept through its CF_DIB bytes.
                if isinstance(data, (bytes, str)):
                    old[fmt] = data
        cb.EmptyClipboard()
        if text is not None:
            cb.SetClipboardData(cb.CF_UNICODETEXT, text)
        else:
            for fmt, data in (saved or {}).items():
                try:
                    cb.SetClipboardData(fmt, data)
                except Exception:  # noqa: BLE001
                    continue
        return old
    finally:
        cb.CloseClipboard()


def _hr(e: comtypes.COMError) -> int:
    return e.hresult if hasattr(e, "hresult") else (e.args[0] if e.args else 0)


class WinNativeBackend(Backend):
    platform = "windows"
    settle_quiet_s = 0.3
    settle_poll_s = 0.05

    def __init__(self, settings: Settings):
        self.settings = settings
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="winuia", initializer=init_thread)
        self.reader: UIAReader | None = None
        self.app_name = ""
        self.pids: set[int] = set()
        self.hwnd: int | None = None
        self._snap: Snapshot | None = None
        self._bound: TargetInfo | None = None
        self._shot_warned = False
        self._shot: tuple[tuple[int, int], tuple[float, float, float, float]] | None = None  # (screenshot size, frame)
        self.last_timing: dict[str, float] = {}

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    # ---- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        if not winwin.interactive_session():
            log.warning("the daemon runs in session 0 (a service or sandbox): there is no desktop to read or drive; "
                        "start it from a normal terminal in your Windows session")

    async def stop(self) -> None:
        self._exec.shutdown(wait=False)

    async def reset(self) -> None:
        self._snap = None

    # ---- targets ------------------------------------------------------------------------
    async def list_targets(self) -> list[TargetInfo]:
        return await self._run(winwin.list_targets)

    async def bind(self, target: TargetInfo) -> Observation:
        name = str(target.app or target.id)
        hwnd = int(target.id) if target.kind == "window" and isinstance(target.id, int) else None
        if hwnd is None:
            hwnd = await self._run(winwin.find_window, name)
        if hwnd is None:
            if not await self._run(winwin.app_running, name):
                if not await self._run(winwin.launch, name):
                    raise RuntimeError_(f"could not find or launch the app {name!r}")
            deadline = time.time() + 15
            while hwnd is None and time.time() < deadline:
                await asyncio.sleep(0.3)
                hwnd = await self._run(winwin.find_window, name)
        if hwnd is None:
            raise RuntimeError_(f"could not find or launch the app {name!r}")
        await self._run(self._attach, hwnd, name)
        self._bound = target
        deadline = time.time() + 15
        while True:
            try:
                return await self.observe_ax()
            except LookupError:
                if time.time() > deadline:
                    raise RuntimeError_(f"{self.app_name} has no window to bind")
                await asyncio.sleep(0.3)

    def _attach(self, hwnd: int, name: str) -> None:
        self.hwnd = hwnd
        self.pids = {winwin.window_pid(hwnd), winwin.hosted_pid(hwnd)} - {0}
        self.app_name = winwin.app_name_of(hwnd) or name
        self.reader = UIAReader(self._window, self.app_name, margin=self.settings.native_margin)

    def _window(self) -> tuple[int | None, list[int]]:
        """The window to read now and its popups (see ``winwin.resolve_window``)."""
        h = winwin.resolve_window(self.hwnd, self.pids)
        if h is None:
            return None, []
        self.hwnd = h
        return h, winwin.popups_of(h)

    # ---- observation ----------------------------------------------------------------------
    def _need(self) -> UIAReader:
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

    def _capture(self) -> tuple[bytes, tuple[float, float, float, float]]:
        self._need()
        if not self.hwnd:
            raise RuntimeError_(f"{self.app_name} has no window to capture")
        try:
            return winwin.capture_window(self.hwnd)
        except winwin.CaptureError as e:
            raise RuntimeError_(f"screenshot failed: {e}") from e

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

    def _com(self, what: str, fn, *args) -> Any:
        try:
            return fn(*args)
        except comtypes.COMError as e:
            if _hr(e) in INVALID_HRESULTS:
                raise ElementError(f"{what}: element is gone or disabled ({_hr(e):#x})") from e
            raise RuntimeError_(f"{what} failed ({_hr(e) & 0xFFFFFFFF:#x})") from e

    def _pattern(self, ref: Any, pattern_id: int, iface: Any) -> Any:
        try:
            p = ref.GetCurrentPattern(pattern_id)
        except comtypes.COMError as e:
            if _hr(e) in INVALID_HRESULTS:
                raise ElementError(f"element is gone ({_hr(e):#x})") from e
            return None
        return p.QueryInterface(iface) if p else None

    def _activate(self) -> None:
        """Bring the bound window forward: synthesized input goes to the foreground window."""
        if not self.hwnd:
            raise RuntimeError_("no bound window")
        if not winwin.activate(self.hwnd):
            raise RuntimeError_(f"could not bring {self.app_name} to the foreground (Windows refused the switch)")

    def _owns_point(self, x: float, y: float) -> bool:
        """True when the bound app owns the window on screen at (x, y)."""
        root = winwin.root_at(x, y)
        return bool(root) and (winwin.window_pid(root) in self.pids or winwin.hosted_pid(root) in self.pids)

    def _center(self, ref: Any) -> tuple[float, float]:
        """On-screen center of ``ref``, scrolling it into view first when needed."""
        reader = self._need()
        wf = winwin.window_frame(self.hwnd) if self.hwnd else None

        def hidden(fr: Any) -> bool:
            return not fr or fr[2] <= 2 or fr[3] <= 2 or bool(wf and not (wf[1] <= fr[1] + fr[3] / 2 <= wf[1] + wf[3]))

        f = reader.frame_of(ref)
        if hidden(f):
            item = self._pattern(ref, U.UIA_ScrollItemPatternId, U.IUIAutomationScrollItemPattern)
            if item is not None:
                try:
                    item.ScrollIntoView()
                    time.sleep(0.15)
                except comtypes.COMError:
                    pass
            f = reader.frame_of(ref)
        if hidden(f):
            raise ElementError("element has no on-screen frame to click")
        return f[0] + f[2] / 2, f[1] + f[3] / 2

    def _hit_is(self, ref: Any, x: float, y: float) -> bool:
        """True when the element under (x, y) is ``ref`` or inside it: a pointer click lands on the
        grounded element and nothing else."""
        uia = self._need().uia
        try:
            el = uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
            walker = uia.ControlViewWalker
            for _ in range(12):
                if el is None:
                    return False
                if uia.CompareElements(el, ref):
                    return True
                el = walker.GetParentElement(el)
        except comtypes.COMError:
            return False
        return False

    def _mouse_click(self, x: float, y: float, count: int = 1, button: str = "left") -> None:
        self._activate()
        if not self._owns_point(x, y):
            time.sleep(0.2)
            if not self._owns_point(x, y):
                raise RuntimeError_(f"another window covers ({x:.0f},{y:.0f}); refusing a pointer click that would land outside {self.app_name}")
        saved = win32api_cursor()
        down, up = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right" else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
        try:
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.03)
            for _ in range(count):
                _send(_mouse_input(down), _mouse_input(up))
                time.sleep(0.02)
            time.sleep(0.05)
        finally:
            user32.SetCursorPos(*saved)

    def _focus(self, ref: Any) -> None:
        self._com("focus", ref.SetFocus)

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
        assert self._snap is not None
        acts = self._snap.actions.get(index, set())
        if not mouse and count == 1 and button == "left":
            if index in self._snap.text_inputs:
                self._focus(ref)
                return "uia_focus"
            how = self._press(ref, acts, self._snap.frameworks.get(index, ""))
            if how:
                return how
        x, y = self._center(ref)
        self._activate()
        if not self._hit_is(ref, x, y):
            item = self._pattern(ref, U.UIA_ScrollItemPatternId, U.IUIAutomationScrollItemPattern)
            if item is not None:
                try:
                    item.ScrollIntoView()
                except comtypes.COMError:
                    pass
                time.sleep(0.15)
            x, y = self._center(ref)
            if not self._hit_is(ref, x, y):
                raise ElementError("something else is on top of the target at its on-screen position")
        self._mouse_click(x, y, count, button)
        return "mouse"

    def _press(self, ref: Any, acts: set[str], framework: str) -> str | None:
        """Click through the first pattern the element advertises. A pattern can be advertised and still
        refuse the call (InvalidOperation, not supported); then None, and the caller clicks for real."""
        def expand(p: Any) -> None:
            (p.Collapse if p.CurrentExpandCollapseState == 1 else p.Expand)()

        tries = [
            ("invoke", U.UIA_InvokePatternId, U.IUIAutomationInvokePattern, lambda p: p.Invoke()),
            ("toggle", U.UIA_TogglePatternId, U.IUIAutomationTogglePattern, lambda p: p.Toggle()),
            ("select", U.UIA_SelectionItemPatternId, U.IUIAutomationSelectionItemPattern, lambda p: p.Select()),
            ("expand", U.UIA_ExpandCollapsePatternId, U.IUIAutomationExpandCollapsePattern, expand),
        ]
        for name, pid, iface, call in tries:
            if name not in acts or (name == "select" and framework not in SELECT_FRAMEWORKS):
                continue
            p = self._pattern(ref, pid, iface)
            if p is None:
                continue
            try:
                call(p)
            except comtypes.COMError as e:
                if _hr(e) == UIA_E_ELEMENTNOTAVAILABLE:
                    raise ElementError(f"{name}: element is gone") from e
                log.debug("%s pattern refused (%#x); clicking with the pointer", name, _hr(e) & 0xFFFFFFFF)
                return None
            return f"uia_{name}"
        return None

    def _do_set_value(self, index: int, value: str) -> None:
        ref = self._ref(index)
        p = self._pattern(ref, U.UIA_ValuePatternId, U.IUIAutomationValuePattern)
        if p is None or self._com("read-only check", lambda: p.CurrentIsReadOnly):
            raise ElementError("Cannot set a value for an element that is not settable")
        self._com("set value", p.SetValue, value)

    def _do_paste(self, text: str, index: int | None) -> None:
        self._activate()  # synthesized keys go to the foreground window
        if index is not None:
            self._focus(self._ref(index))
            time.sleep(0.03)
        saved = _clipboard_swap(text)
        try:
            send_chord(VK["v"], [VK_CONTROL])
            time.sleep(0.25)  # the app reads the clipboard asynchronously
        finally:
            _clipboard_swap(None, saved)

    def _do_type(self, text: str, index: int | None) -> None:
        self._activate()
        if index is not None:
            self._focus(self._ref(index))
            time.sleep(0.03)
        send_text(text)

    def _do_key(self, key: str, index: int | None) -> None:
        try:
            vk, mods = parse_chord(key)
        except ValueError as e:
            raise RuntimeError_(str(e)) from e
        self._activate()
        if index is not None:
            self._focus(self._ref(index))
        send_chord(vk, mods)

    def _do_scroll(self, direction: str, amount: int, index: int | None, xy: tuple[int, int] | None) -> str:
        """Scroll by ``amount`` pages: the Scroll pattern of the element or its nearest scrollable
        ancestor (background-safe), else ScrollItem paging, else the wheel."""
        if xy is None:
            r = self._pattern_scroll(direction, amount, index)
            if r in ("uia", "uia_edge"):
                return r
            if direction in ("up", "down"):
                how = "item"
                for _ in range(amount):
                    r = self._item_page(direction, index)
                    if r == "edge":
                        return "item_edge" if how == "item" else how
                    if r == "none":
                        how = "wheel"
                        break
                else:
                    return how
        self._wheel_scroll(direction, amount, index, xy)
        return "wheel"

    def _scroll_owner(self, index: int | None) -> Any:
        """The element whose Scroll pattern moves ``index`` (or the page): itself or its nearest
        scrollable ancestor; for the page, the web document if any, else the first scrollable element."""
        snap = self._snap
        assert snap is not None
        if index is None:
            web = next((i for i, c in snap.ctypes.items() if c == "Document" and "scroll" in snap.actions.get(i, set())), None)
            if web is not None:
                return snap.refs[web]
            first = next((i for i in sorted(snap.refs) if "scroll" in snap.actions.get(i, set())), None)
            return snap.refs[first] if first is not None else None
        cur: int | None = index
        while cur is not None:
            if "scroll" in snap.actions.get(cur, set()):
                return snap.refs[cur]
            cur = snap.parents.get(cur)
        return None

    def _pattern_scroll(self, direction: str, amount: int, index: int | None) -> str:
        if self._snap is None:
            self._snap = self._need().read("action")
        owner = self._scroll_owner(index)
        if owner is None:
            return "none"
        p = self._pattern(owner, U.UIA_ScrollPatternId, U.IUIAutomationScrollPattern)
        if p is None:
            return "none"
        vertical = direction in ("up", "down")
        try:
            can = p.CurrentVerticallyScrollable if vertical else p.CurrentHorizontallyScrollable
            if not can:
                return "none"
            pct = lambda: p.CurrentVerticalScrollPercent if vertical else p.CurrentHorizontalScrollPercent  # noqa: E731
            before = pct()
            forward = direction in ("down", "right")
            if (forward and before >= 99.99) or (not forward and 0 <= before <= 0.01):
                return "uia_edge"
            inc = U.ScrollAmount_LargeIncrement if forward else U.ScrollAmount_LargeDecrement
            for _ in range(amount):
                if vertical:
                    p.Scroll(U.ScrollAmount_NoAmount, inc)
                else:
                    p.Scroll(inc, U.ScrollAmount_NoAmount)
                time.sleep(0.03)
        except comtypes.COMError:
            return "none"
        time.sleep(0.05)
        return "uia"

    def _item_page(self, direction: str, index: int | None) -> str:
        """One page via ScrollItem.ScrollIntoView on the item ~0.8 pages past the edge.
        Returns "ok", "edge" (nothing further) or "none"."""
        from ..ax import parse_ax

        reader = self._need()
        ref = self._ref(index) if index is not None else None
        snap = reader.read("action")
        tree = parse_ax(snap.text)
        if ref is not None:
            same = lambda i: self._com("compare", reader.uia.CompareElements, snap.refs[i], ref)  # noqa: E731
            root = next((e for e in tree.elements if same(e.index)), None)
        else:  # the page: web content if any, else the window
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
        if "scroll_item" not in snap.actions.get(target.index, set()):
            return "none"
        item = self._pattern(snap.refs[target.index], U.UIA_ScrollItemPatternId, U.IUIAutomationScrollItemPattern)
        try:
            item.ScrollIntoView()
        except (comtypes.COMError, AttributeError):
            return "none"
        time.sleep(0.05)
        return "ok"

    def _wheel_scroll(self, direction: str, amount: int, index: int | None, xy: tuple[int, int] | None) -> None:
        """Real wheel events; they go to the window under the pointer, so the window is brought
        forward, the pointer parked over the target and restored afterwards."""
        reader = self._need()
        wf = winwin.window_frame(self.hwnd) if self.hwnd else None
        if index is not None:
            f = reader.frame_of(self._ref(index))
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
        notches = max(1, round(page * amount / 100))  # ~100 px per notch (3 lines)
        sign = 1 if direction in ("up", "right") else -1  # positive wheel data scrolls up / right
        flag = MOUSEEVENTF_WHEEL if direction in ("up", "down") else MOUSEEVENTF_HWHEEL
        self._activate()
        if not self._owns_point(x, y):
            raise RuntimeError_(f"another window covers ({x:.0f},{y:.0f}); refusing to scroll outside {self.app_name}")
        saved = win32api_cursor()
        try:
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.02)
            for _ in range(notches):
                _send(_mouse_input(flag, sign * WHEEL_DELTA))
                time.sleep(0.012)
            time.sleep(0.05)
        finally:
            user32.SetCursorPos(*saved)

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
        return await self._after(t0, "uia_value")

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


def win32api_cursor() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def native_available() -> tuple[bool, str]:
    """Whether the native backend can run here, and why not."""
    try:
        from . import winuia  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"UI Automation is unavailable ({e}); pip install comtypes pywin32"
    if not winwin.interactive_session():
        return False, ("this process runs in session 0 (a service or sandbox) with no desktop; "
                       "run Voyager from a terminal in your Windows session")
    return True, ""
