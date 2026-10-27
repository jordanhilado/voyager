"""Native Windows UI Automation reader (comtypes ``IUIAutomation``).

Reads the bound window's tree with one cross-process call (``ElementFromHandleBuildCache`` over
the control view, every property cached) and prints it in the text format ``ax.parse_ax`` reads.
Every element also keeps its screen frame (physical pixels; the daemon is per-monitor DPI aware)
and its live ``IUIAutomationElement``, which the native actions use.

Shape rules match the macOS reader: groups and panes print as ``container``, unnamed single-child
containers collapse into their child, empty non-interactive leaves are dropped, and a text leaf
that only repeats its parent's label is dropped.

Roles: control types map to the role names the macOS reader prints, so the parser, grounding and
policy see the same vocabulary on both platforms. A web document (Chromium or Gecko) prints as
``HTML content`` with its URL; ``Edit`` with ``IsPassword`` prints as ``secure text field``.

Scope:
- ``action``: elements UIA reports on screen (the provider skips the rest, which more than halves
  the cost of a browser read), within ``margin`` window-heights of the window. Anything scrolled
  out of view is reached with a ``find`` step or a ``full`` re-read.
- ``full``: the whole window.

The bound window's owned popups (dialogs, menus, dropdowns of the same process) are read as extra
roots after the window, the way a macOS sheet is part of its window.

All calls are COM calls on the backend's single worker thread (initialized multithreaded).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import comtypes
import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as U  # noqa: E402

# ---- control types and roles ------------------------------------------------------------------
CT = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}
ROLE_NAMES = {
    "Button": "button", "Calendar": "table", "CheckBox": "check box", "ComboBox": "combo box",
    "Edit": "text field", "Hyperlink": "link", "Image": "image", "ListItem": "list item", "List": "list",
    "Menu": "menu", "MenuBar": "menu bar", "MenuItem": "menu item", "ProgressBar": "progress indicator",
    "RadioButton": "radio button", "ScrollBar": "scroll bar", "Slider": "slider", "Spinner": "incrementor",
    "StatusBar": "status bar", "Tab": "tab group", "TabItem": "tab", "Text": "text", "ToolBar": "toolbar",
    "ToolTip": "tool tip", "Tree": "outline", "TreeItem": "tree item", "Custom": "container",
    "Group": "container", "Thumb": "handle", "DataGrid": "table", "DataItem": "row", "Document": "document",
    "SplitButton": "menu button", "Window": "standard window", "Pane": "container", "Header": "container",
    "HeaderItem": "column", "Table": "table", "TitleBar": "container", "Separator": "separator",
    "SemanticZoom": "container", "AppBar": "toolbar",
}
# Control types that stay in the tree even with no text.
INTERACTIVE_CT = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem", "RadioButton", "Slider",
    "Spinner", "TabItem", "TreeItem", "DataItem", "SplitButton", "HeaderItem", "Tab",
}
TEXT_INPUT_CT = {"Edit", "ComboBox"}
CONTAINER_CT = {"Group", "Pane", "Custom", "Header", "TitleBar", "SemanticZoom"}
TEXT_CT = {"Text"}
WEB_FRAMEWORKS = {"Chrome", "Gecko"}
# Browsers name list, tree and grid items after their content, so <li><a>Privacy policy</a></li> would list
# the item next to its link as a second "Privacy policy" target. A web item is a target only when it
# advertises an action of its own; otherwise it prints as a container (as Chromium's items do on macOS).
WEB_ITEM_CT = {"ListItem", "TreeItem", "DataItem"}
ITEM_ACTIONS = {"invoke", "select", "toggle", "expand"}
MAX_NODES = {"action": 6000, "full": 30000}
MAX_NAME = 4000
MAX_WALK_DEPTH = 120

# Cached on every element. Pattern-specific values come back as "not supported" where they do not apply.
P = U
PROPS = [
    P.UIA_ControlTypePropertyId, P.UIA_NamePropertyId, P.UIA_AutomationIdPropertyId, P.UIA_ClassNamePropertyId,
    P.UIA_BoundingRectanglePropertyId, P.UIA_IsEnabledPropertyId, P.UIA_IsPasswordPropertyId,
    P.UIA_HelpTextPropertyId, P.UIA_FrameworkIdPropertyId, P.UIA_LocalizedControlTypePropertyId,
    P.UIA_HasKeyboardFocusPropertyId, P.UIA_IsInvokePatternAvailablePropertyId,
    P.UIA_IsTogglePatternAvailablePropertyId, P.UIA_IsSelectionItemPatternAvailablePropertyId,
    P.UIA_IsExpandCollapsePatternAvailablePropertyId, P.UIA_IsValuePatternAvailablePropertyId,
    P.UIA_IsScrollPatternAvailablePropertyId, P.UIA_IsScrollItemPatternAvailablePropertyId,
    P.UIA_ValueValuePropertyId, P.UIA_ValueIsReadOnlyPropertyId, P.UIA_ToggleToggleStatePropertyId,
    P.UIA_SelectionItemIsSelectedPropertyId, P.UIA_ExpandCollapseExpandCollapseStatePropertyId,
    P.UIA_ScrollVerticallyScrollablePropertyId, P.UIA_ScrollVerticalScrollPercentPropertyId,
]
IS_DIALOG = getattr(P, "UIA_IsDialogPropertyId", 30174)
PROPS.append(IS_DIALOG)

# Errors that mean the element is gone or not usable: re-ground.
UIA_E_ELEMENTNOTAVAILABLE = -2147220991  # 0x80040201
UIA_E_ELEMENTNOTENABLED = -2147220992  # 0x80040200
UIA_E_NOTSUPPORTED = -2147220990  # 0x80040202 (pattern not supported)
INVALID_HRESULTS = {UIA_E_ELEMENTNOTAVAILABLE, UIA_E_ELEMENTNOTENABLED, -2147467259}  # + E_FAIL


Frame = tuple[float, float, float, float]  # x, y, w, h in physical screen pixels


def intersects(a: Frame, b: Frame) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _prim(v: Any) -> Any:
    """A cached value, or None when the property is not supported (UIA returns a sentinel object)."""
    return v if isinstance(v, (str, bool, int, float, tuple)) else None


def _str(v: Any) -> str | None:
    v = _prim(v)
    if v is None or isinstance(v, (bool, tuple)):
        return None
    return v if isinstance(v, str) else str(v)


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return s[:MAX_NAME]


def _rect(v: Any) -> Frame | None:
    v = _prim(v)
    if not isinstance(v, tuple) or len(v) != 4:
        return None
    return (float(v[0]), float(v[1]), float(v[2]), float(v[3]))


_uia: Any = None


def automation() -> Any:
    """The ``IUIAutomation`` object for this thread (one per process; created on first use)."""
    global _uia
    if _uia is None:
        _uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    return _uia


def init_thread() -> None:
    """Worker-thread initializer: UIA clients should run in the multithreaded apartment."""
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # already initialized on this thread


@dataclass
class Node:
    ref: Any
    ctype: str
    role: str
    name: str = ""
    desc: str = ""
    value: str | None = None
    ident: str | None = None
    url: str | None = None
    help: str | None = None
    placeholder: str | None = None
    flags: list[str] = field(default_factory=list)
    frame: Frame | None = None
    children: list["Node"] = field(default_factory=list)
    focused: bool = False
    text_input: bool = False
    framework: str = ""
    actions: set[str] = field(default_factory=set)  # see PATTERN_ACTIONS
    live: bool = False  # this node or a descendant is interactive
    passive: bool = False  # an item control that only wraps content (see WEB_ITEM_CT)

    @property
    def texts(self) -> list[str]:
        return [t for t in (self.name, self.desc, self.value if not self.text_input else None) if t]

    @property
    def interactive(self) -> bool:
        return (self.ctype in INTERACTIVE_CT and not self.passive) or "settable" in self.flags


@dataclass
class Snapshot:
    text: str
    refs: dict[int, Any]
    frames: dict[int, Frame]
    window_frame: Frame | None
    nodes: int  # UIA nodes visited
    ms: float
    partial: bool
    # Per index: which actions the element advertises ("invoke", "toggle", "select", "expand",
    # "value", "scroll", "scroll_item"), its control type, and whether it is a text input.
    actions: dict[int, set[str]] = field(default_factory=dict)
    ctypes: dict[int, str] = field(default_factory=dict)
    frameworks: dict[int, str] = field(default_factory=dict)  # UIA FrameworkId: "Win32", "XAML", "Chrome", ...
    text_inputs: set[int] = field(default_factory=set)
    parents: dict[int, int] = field(default_factory=dict)


class UIAReader:
    """Reads one app's window. Not thread-safe: use from one worker thread.

    ``window_fn`` returns ``(main hwnd, [popup hwnds])`` for the current read; the backend owns the
    rules for which window that is (see ``winwin.resolve_window``)."""

    def __init__(self, window_fn, app_name: str, margin: float = 1.0):
        self.window_fn = window_fn
        self.app_name = app_name
        self.margin = margin
        self.uia = automation()
        self.hwnd: int | None = None
        self.window: Any = None  # the main window's element from the last read
        self._cache = {"full": self._cache_request(False), "action": self._cache_request(True)}
        self._visited = 0
        self._cap = MAX_NODES["action"]

    def _cache_request(self, onscreen: bool) -> Any:
        """``onscreen``: skip elements UIA reports offscreen (scrolled out of view, collapsed). The
        provider then sends no properties for them: on a Wikipedia article in Edge, 184 ms and 416
        nodes instead of 433 ms and 3,722."""
        cr = self.uia.CreateCacheRequest()
        for p in PROPS:
            try:
                cr.AddProperty(p)
            except comtypes.COMError:
                pass  # property unknown on this Windows build
        cr.TreeScope = U.TreeScope_Subtree
        cond = self.uia.ControlViewCondition
        if onscreen:
            cond = self.uia.CreateAndCondition(cond, self.uia.CreatePropertyCondition(U.UIA_IsOffscreenPropertyId, False))
        cr.TreeFilter = cond
        cr.AutomationElementMode = U.AutomationElementMode_Full
        return cr

    # ---- window -------------------------------------------------------------------------
    def frame_of(self, ref: Any) -> Frame | None:
        """Live frame of an element."""
        try:
            r = ref.CurrentBoundingRectangle
        except comtypes.COMError:
            return None
        w, h = r.right - r.left, r.bottom - r.top
        return (float(r.left), float(r.top), float(w), float(h)) if w > 0 and h > 0 else None

    def element_for(self, hwnd: int) -> Any:
        return self.uia.ElementFromHandle(hwnd)

    # ---- read ---------------------------------------------------------------------------
    def read(self, scope: str = "action") -> Snapshot:
        t0 = time.perf_counter()
        hwnd, popups = self.window_fn()
        if not hwnd:
            raise LookupError(f"{self.app_name} has no window")
        try:
            win = self.uia.ElementFromHandleBuildCache(hwnd, self._cache["full" if scope == "full" else "action"])
        except comtypes.COMError as e:
            raise LookupError(f"{self.app_name}: window {hwnd} is not readable ({e})") from e
        self.hwnd, self.window = hwnd, win
        wf = _rect(win.GetCachedPropertyValue(U.UIA_BoundingRectanglePropertyId))
        region: Frame | None = None
        if scope != "full" and wf is not None:
            pad = wf[3] * self.margin
            region = (wf[0], wf[1] - pad, wf[2], wf[3] + 2 * pad)
        self._visited = 0
        self._cap = MAX_NODES["full" if scope == "full" else "action"]
        root = self._walk(win, region, 0)
        roots = [root] if root is not None else []
        for p in popups:
            try:
                pe = self.uia.ElementFromHandleBuildCache(p, self._cache["full"])
            except comtypes.COMError:
                continue
            pn = self._walk(pe, None, 0)
            if pn is not None:
                roots.append(pn)
        title = (root.name if root else "") or ""
        lines = [f'Window: "{title}", App: {self.app_name}.']
        snap = Snapshot("", {}, {}, wf, 0, 0.0, region is not None)
        focused: list[int] = []
        counter = [0]

        def emit(n: Node, depth: int, parent: int | None) -> None:
            i = counter[0]
            counter[0] += 1
            snap.refs[i] = n.ref
            if n.frame:
                snap.frames[i] = n.frame
            snap.ctypes[i] = n.ctype
            snap.frameworks[i] = n.framework
            snap.actions[i] = n.actions
            if n.text_input:
                snap.text_inputs.add(i)
            if parent is not None:
                snap.parents[i] = parent
            if n.focused:
                focused.append(i)
            lines.append("\t" * depth + f"{i} " + _format(n))
            for c in n.children:
                emit(c, depth + 1, i)

        for r in roots:
            emit(r, 0, None)
        if focused:
            fi = focused[-1]  # the innermost focused element
            lines.append(f"The focused UI element is {fi} {lines[fi + 1].lstrip(chr(9)).split(' ', 1)[1]}")
        snap.text = "\n".join(lines) + "\n"
        snap.nodes = self._visited
        snap.ms = (time.perf_counter() - t0) * 1000
        return snap

    def _walk(self, el: Any, region: Frame | None, depth: int) -> Node | None:
        # The depth cap guards against trees that loop back on themselves.
        if self._visited >= self._cap or depth > MAX_WALK_DEPTH:
            return None
        self._visited += 1
        g = el.GetCachedPropertyValue
        try:
            frame = _rect(g(U.UIA_BoundingRectanglePropertyId))
        except comtypes.COMError:
            return None
        if frame is not None and (frame[2] <= 0 or frame[3] <= 0):
            frame = None
        if region is not None and depth >= 2 and frame and not intersects(frame, region):
            return None
        n = _node(el, g, frame)
        try:
            kids = el.GetCachedChildren()
        except comtypes.COMError:
            kids = None
        if kids:  # a leaf's cached children come back as a NULL pointer
            for i in range(kids.Length):
                cn = self._walk(kids.GetElement(i), region, depth + 1)
                if cn is not None:
                    n.children.append(cn)
        return _prune(n, depth)


PATTERN_ACTIONS = (
    ("invoke", U.UIA_IsInvokePatternAvailablePropertyId), ("toggle", U.UIA_IsTogglePatternAvailablePropertyId),
    ("select", U.UIA_IsSelectionItemPatternAvailablePropertyId), ("expand", U.UIA_IsExpandCollapsePatternAvailablePropertyId),
    ("value", U.UIA_IsValuePatternAvailablePropertyId), ("scroll", U.UIA_IsScrollPatternAvailablePropertyId),
    ("scroll_item", U.UIA_IsScrollItemPatternAvailablePropertyId),
)


def _node(el: Any, g, frame: Frame | None) -> Node:
    def get(pid: int) -> Any:
        try:
            return _prim(g(pid))
        except comtypes.COMError:
            return None

    ctype = CT.get(get(U.UIA_ControlTypePropertyId) or 0, "Custom")
    name = _clean(_str(get(U.UIA_NamePropertyId)))
    if ctype in TEXT_CT:
        # Most nodes of a web page are text: read only what a text line prints (and ScrollItem, which paging uses).
        lct = (_str(get(U.UIA_LocalizedControlTypePropertyId)) or "").lower()
        n = Node(el, ctype, "heading" if lct == "heading" else "text", name=name,
                 ident=_clean(_str(get(U.UIA_AutomationIdPropertyId))) or None, frame=frame)
        if get(U.UIA_IsScrollItemPatternAvailablePropertyId) is True:
            n.actions.add("scroll_item")
        return n
    actions = {a for a, pid in PATTERN_ACTIONS if get(pid) is True}
    framework = _str(get(U.UIA_FrameworkIdPropertyId)) or ""
    lct = (_str(get(U.UIA_LocalizedControlTypePropertyId)) or "").lower()
    value_ok = "value" in actions
    raw_value = _str(get(U.UIA_ValueValuePropertyId)) if value_ok else None
    settable = value_ok and get(U.UIA_ValueIsReadOnlyPropertyId) is False
    toggle = get(U.UIA_ToggleToggleStatePropertyId) if "toggle" in actions else None
    selectable = "select" in actions

    role = ROLE_NAMES.get(ctype, "unknown")
    passive = False
    text_input = ctype in TEXT_INPUT_CT and (ctype == "Edit" or settable)
    if ctype == "Edit" and get(U.UIA_IsPasswordPropertyId) is True:
        role = "secure text field"
    elif ctype == "Document":
        if framework in WEB_FRAMEWORKS:
            role = "HTML content"
        elif settable:
            role, text_input = "text entry area", True
    elif ctype in WEB_ITEM_CT and framework in WEB_FRAMEWORKS and not actions & ITEM_ACTIONS:
        role, passive = "container", True
    elif ctype == "Button" and toggle is not None:
        role = "toggle button"
    if get(IS_DIALOG) is True or (ctype in ("Window", "Pane", "Group") and lct == "dialog"):
        role = "dialog"

    desc = _clean(_str(get(U.UIA_HelpTextPropertyId)))
    value: str | None = None
    url: str | None = None
    if text_input or ctype in {"ComboBox", "Slider", "Spinner", "ProgressBar"}:
        value = _clean(raw_value) or None
    elif ctype == "Hyperlink":
        value = _clean(raw_value) or None  # a link's target prints as its Value
    elif role == "HTML content":
        url = _clean(raw_value) or None
    flags: list[str] = []
    if get(U.UIA_IsEnabledPropertyId) is False and ctype in INTERACTIVE_CT:
        flags.append("disabled")
    if settable and (text_input or ctype in {"Slider", "Spinner"}):
        flags.append("settable")
    if ctype in {"CheckBox", "RadioButton"} or role == "toggle button":
        state = toggle if toggle is not None else (get(U.UIA_SelectionItemIsSelectedPropertyId) if selectable else None)
        if state is not None and state != 2:
            flags.append("checked" if state else "unchecked")
    elif selectable and get(U.UIA_SelectionItemIsSelectedPropertyId) is True:
        flags.append("selected")
    if "expand" in actions and get(U.UIA_ExpandCollapseExpandCollapseStatePropertyId) == 1:
        flags.append("expanded")
    return Node(
        el, ctype, role, name=name, desc=desc if desc != name else "", value=value,
        ident=_clean(_str(get(U.UIA_AutomationIdPropertyId))) or None, url=url, flags=flags,
        frame=frame, focused=get(U.UIA_HasKeyboardFocusPropertyId) is True,
        text_input=text_input, framework=framework, actions=actions, passive=passive,
    )


def _prune(n: Node, depth: int) -> Node | None:
    labels = " ".join(n.texts)
    n.live = n.interactive or any(c.live for c in n.children)
    if labels and not any(c.live for c in n.children):
        # A text leaf that only repeats its parent's label adds nothing (link "Home" > text "Home").
        # Kept when siblings are interactive: then the leaves carry the reading order.
        n.children = [c for c in n.children if not (c.ctype in TEXT_CT and not c.children and c.name and c.name in labels)]
    if depth == 0:
        return n
    if not n.children and not labels and not n.interactive and n.role != "HTML content" and not n.focused:
        return None
    if n.ctype in CONTAINER_CT and n.role == "container" and not labels and not n.interactive and not n.ident and len(n.children) == 1:
        return n.children[0]
    return n


def _format(n: Node) -> str:
    parts = n.role
    if n.flags:
        parts += f" ({', '.join(n.flags)})"
    if n.name:
        parts += f" {n.name}"
    attrs: list[str] = []
    if n.value:
        attrs.append(f"Value: {n.value}")
    if n.desc:
        attrs.append(f"Description: {n.desc}")
    if n.ident:
        attrs.append(f"ID: {n.ident}")
    if n.url:
        attrs.append(f"URL: {n.url}")
    if n.help:
        attrs.append(f"Help: {n.help}")
    elif n.placeholder:
        attrs.append(f"Placeholder: {n.placeholder}")
    if attrs:
        parts += ("," if n.name else "") + " " + ", ".join(attrs)
    return parts
