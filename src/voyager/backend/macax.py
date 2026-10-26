"""Native macOS accessibility reader (pyobjc ``AXUIElement``).

Reads the bound window's tree directly and prints it in the text format ``ax.parse_ax`` reads.
Every element also keeps its screen frame and its live ``AXUIElement`` reference, which the
native actions use.

Shape rules: groups print as ``container``, unnamed single-child
containers collapse into their child, empty non-interactive leaves are dropped, and a text leaf
that only repeats its parent's label is dropped.

Scope:
- ``action``: elements whose frame is within ``margin`` window-heights of the window. Used by the
  per-step loop (settle, fused calls, grounding). Anything further away is reached with a
  ``find`` step or a ``full`` re-read.
- ``full``: the whole window, for reads and as the grounding fallback.

All calls are synchronous AX IPC; the backend runs them on one worker thread.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import ApplicationServices as AS

from ..ax import KNOWN_ROLES

_KNOWN = set(KNOWN_ROLES)

ATTRS = [
    "AXRole", "AXSubrole", "AXRoleDescription", "AXTitle", "AXDescription", "AXValue",
    "AXIdentifier", "AXDOMIdentifier", "AXURL", "AXHelp", "AXPlaceholderValue", "AXEnabled",
    "AXPosition", "AXSize", "AXChildren", "AXSelected", "AXExpanded",
]

ROLE_NAMES = {
    "AXWindow": "standard window", "AXSheet": "sheet", "AXDrawer": "drawer", "AXPopover": "popover",
    "AXGroup": "container", "AXScrollArea": "scroll area", "AXWebArea": "HTML content",
    "AXStaticText": "text", "AXHeading": "heading", "AXLink": "link", "AXButton": "button",
    "AXMenuButton": "menu button", "AXPopUpButton": "pop up button", "AXCheckBox": "check box",
    "AXRadioButton": "radio button", "AXRadioGroup": "radio group", "AXTextField": "text field",
    "AXTextArea": "text entry area", "AXSearchField": "search field", "AXComboBox": "combo box",
    "AXSlider": "slider", "AXIncrementor": "incrementor", "AXImage": "image", "AXList": "list",
    "AXOutline": "outline", "AXTable": "table", "AXRow": "row", "AXCell": "cell", "AXColumn": "column",
    "AXTabGroup": "tab group", "AXToolbar": "toolbar", "AXMenuBar": "menu bar", "AXMenuBarItem": "menu bar item",
    "AXMenu": "menu", "AXMenuItem": "menu item", "AXSplitGroup": "split group", "AXSplitter": "splitter",
    "AXProgressIndicator": "progress indicator", "AXBusyIndicator": "busy indicator",
    "AXDisclosureTriangle": "disclosure triangle", "AXScrollBar": "scroll bar", "AXCollection": "collection",
    "AXDateField": "date field", "AXTimeField": "time field", "AXColorWell": "color well",
    "AXLevelIndicator": "level indicator", "AXValueIndicator": "value indicator", "AXHelpTag": "help tag",
    "AXLayoutArea": "layout area", "AXLayoutItem": "layout item", "AXHandle": "handle", "AXUnknown": "unknown",
}
SUBROLE_NAMES = {
    "AXSecureTextField": "secure text field", "AXSearchField": "search field", "AXDialog": "dialog",
    "AXSystemDialog": "dialog", "AXCloseButton": "close button", "AXMinimizeButton": "minimize button",
    "AXFullScreenButton": "full screen button", "AXZoomButton": "zoom button", "AXToggle": "toggle button",
    "AXTabButton": "tab",
}
# Roles that stay in the tree even with no text.
INTERACTIVE_AX = {
    "AXButton", "AXLink", "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXMenuItem", "AXMenuBarItem", "AXSlider",
    "AXIncrementor", "AXDisclosureTriangle", "AXRow", "AXCell", "AXTabGroup", "AXDateField", "AXTimeField",
    "AXColorWell",
}
TEXT_INPUT_AX = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXDateField", "AXTimeField"}
CONTAINER_AX = {"AXGroup", "AXUnknown", "AXLayoutArea", "AXSplitGroup"}
TEXT_AX = {"AXStaticText"}
MAX_NODES = {"action": 6000, "full": 30000}
MAX_NAME = 4000


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    # NSURL / CFURL
    if hasattr(v, "absoluteString"):
        try:
            return str(v.absoluteString())
        except Exception:  # noqa: BLE001
            return None
    return None


def _is_err(v: Any) -> bool:
    try:
        return AS.AXValueGetType(v) == AS.kAXValueAXErrorType
    except Exception:  # noqa: BLE001
        return False


def _point(v: Any) -> tuple[float, float] | None:
    try:
        ok, p = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
        return (float(p.x), float(p.y)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _size(v: Any) -> tuple[float, float] | None:
    try:
        ok, s = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
        return (float(s.width), float(s.height)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return s[:MAX_NAME]


Frame = tuple[float, float, float, float]  # x, y, w, h in screen points


def intersects(a: Frame, b: Frame) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@dataclass
class Node:
    ref: Any
    ax_role: str
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
    live: bool = False  # this node or a descendant is interactive

    @property
    def texts(self) -> list[str]:
        return [t for t in (self.name, self.desc, self.value if self.ax_role not in TEXT_INPUT_AX else None) if t]

    @property
    def interactive(self) -> bool:
        return self.ax_role in INTERACTIVE_AX or "settable" in self.flags


@dataclass
class Snapshot:
    text: str
    refs: dict[int, Any]
    frames: dict[int, Frame]
    window_frame: Frame | None
    nodes: int  # AX nodes visited
    ms: float
    partial: bool


class AXReader:
    """Reads one app's focused window. Not thread-safe: use from one worker thread."""

    def __init__(self, pid: int, app_name: str, margin: float = 1.0, messaging_timeout_s: float = 2.0):
        self.pid = pid
        self.app_name = app_name
        self.margin = margin
        self.app = AS.AXUIElementCreateApplication(pid)
        AS.AXUIElementSetMessagingTimeout(self.app, messaging_timeout_s)
        # Chromium and Electron only build their web accessibility tree when asked to.
        for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            try:
                AS.AXUIElementSetAttributeValue(self.app, attr, True)
            except Exception:  # noqa: BLE001
                pass
        self.window: Any = None
        self._visited = 0
        self._cap = MAX_NODES["action"]

    # ---- window -------------------------------------------------------------------------
    def attr(self, ref: Any, name: str) -> Any:
        err, v = AS.AXUIElementCopyAttributeValue(ref, name, None)
        return None if err or _is_err(v) else v

    def resolve_window(self) -> Any:
        w = self.attr(self.app, "AXFocusedWindow") or self.attr(self.app, "AXMainWindow")
        if w is None:
            ws = self.attr(self.app, "AXWindows") or []
            w = ws[0] if len(ws) else None
        if w is not None:
            self.window = w
        return self.window

    def frame_of(self, ref: Any) -> Frame | None:
        err, vals = AS.AXUIElementCopyMultipleAttributeValues(ref, ["AXPosition", "AXSize"], 0, None)
        if err or not vals:
            return None
        p = _point(vals[0]) if not _is_err(vals[0]) else None
        s = _size(vals[1]) if not _is_err(vals[1]) else None
        return (p[0], p[1], s[0], s[1]) if p and s else None

    # ---- read ---------------------------------------------------------------------------
    def read(self, scope: str = "action") -> Snapshot:
        t0 = time.perf_counter()
        win = self.resolve_window()
        if win is None:
            raise LookupError(f"{self.app_name} has no window")
        wf = self.frame_of(win)
        region: Frame | None = None
        if scope != "full" and wf is not None:
            pad = wf[3] * self.margin
            region = (wf[0], wf[1] - pad, wf[2], wf[3] + 2 * pad)
        self._visited = 0
        self._cap = MAX_NODES["full" if scope == "full" else "action"]
        root = self._walk(win, region, 0)
        roots = [root] if root is not None else []
        menubar = self._menubar()
        if menubar is not None:
            roots.append(menubar)
        title = (root.name if root else "") or ""
        lines = [f'Window: "{title}", App: {self.app_name}.']
        refs: dict[int, Any] = {}
        frames: dict[int, Frame] = {}
        by_ref: dict[Any, int] = {}
        counter = [0]

        def emit(n: Node, depth: int) -> None:
            i = counter[0]
            counter[0] += 1
            refs[i] = n.ref
            if n.frame:
                frames[i] = n.frame
            try:
                by_ref[n.ref] = i
            except TypeError:
                pass
            lines.append("\t" * depth + f"{i} " + _format(n))
            for c in n.children:
                emit(c, depth + 1)

        for r in roots:
            emit(r, 0)
        focused = self.attr(self.app, "AXFocusedUIElement")
        if focused is not None:
            fi = by_ref.get(focused)
            if fi is not None:
                lines.append(f"The focused UI element is {fi} {lines[fi + 1].lstrip(chr(9)).split(' ', 1)[1]}")
        return Snapshot("\n".join(lines) + "\n", refs, frames, wf, self._visited, (time.perf_counter() - t0) * 1000, region is not None)

    def _menubar(self) -> Node | None:
        mb = self.attr(self.app, "AXMenuBar")
        if mb is None:
            return None
        kids = self.attr(mb, "AXChildren") or []
        items = []
        for k in kids:
            t = _clean(_str(self.attr(k, "AXTitle")))
            if t and t != "Apple":
                items.append(Node(k, "AXMenuBarItem", "menu bar item", name=t))
        return Node(mb, "AXMenuBar", "menu bar", children=items) if items else None

    def _walk(self, ref: Any, region: Frame | None, depth: int) -> Node | None:
        # The depth cap guards against accessibility trees that loop back on themselves (seen in Chrome),
        # which would otherwise recurse past Python's limit before the node cap is reached.
        if self._visited >= self._cap or depth > MAX_WALK_DEPTH:
            return None
        self._visited += 1
        err, vals = AS.AXUIElementCopyMultipleAttributeValues(ref, ATTRS, 0, None)
        if err or not vals:
            return None
        d = {k: (None if _is_err(v) else v) for k, v in zip(ATTRS, vals)}
        pos = _point(d["AXPosition"]) if d["AXPosition"] is not None else None
        size = _size(d["AXSize"]) if d["AXSize"] is not None else None
        frame = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        if region is not None and depth >= 2 and frame and frame[2] > 0 and frame[3] > 0 and not intersects(frame, region):
            return None
        ax_role = _str(d["AXRole"]) or "AXUnknown"
        n = _node(ref, ax_role, d, frame)
        if ax_role in TEXT_INPUT_AX or ax_role == "AXSlider":
            err, settable = AS.AXUIElementIsAttributeSettable(ref, "AXValue", None)
            if not err and settable:
                n.flags.append("settable")
        kids = d["AXChildren"]
        if kids is not None:
            for c in kids:
                cn = self._walk(c, region, depth + 1)
                if cn is not None:
                    n.children.append(cn)
        return _prune(n, depth)


MAX_WALK_DEPTH = 120


def _node(ref: Any, ax_role: str, d: dict, frame: Frame | None) -> Node:
    sub = _str(d["AXSubrole"]) or ""
    role = SUBROLE_NAMES.get(sub)
    if role is None:
        rd = _str(d["AXRoleDescription"]) or ""
        role = ROLE_NAMES.get(ax_role) or (rd if rd in _KNOWN else "unknown")
    title = _clean(_str(d["AXTitle"]))
    desc = _clean(_str(d["AXDescription"]))
    raw_value = d["AXValue"]
    name = title
    value: str | None = None
    if ax_role in TEXT_AX:
        name = name or _clean(_str(raw_value))
    elif ax_role in TEXT_INPUT_AX or ax_role in {"AXPopUpButton", "AXSlider", "AXIncrementor", "AXProgressIndicator"}:
        v = _str(raw_value)
        value = _clean(v) if v else None
    url = _clean(_str(d["AXURL"])) or None
    if ax_role == "AXLink" and url:
        value, url = url, None  # a link's target prints as its Value
    flags: list[str] = []
    if d["AXEnabled"] is False and (ax_role in INTERACTIVE_AX):
        flags.append("disabled")
    if ax_role in {"AXCheckBox", "AXRadioButton"} and isinstance(raw_value, int) and not isinstance(raw_value, bool):
        flags.append("checked" if raw_value else "unchecked")
    if d["AXSelected"] is True:
        flags.append("selected")
    if d["AXExpanded"] is True:
        flags.append("expanded")
    ident = _clean(_str(d["AXIdentifier"]) or _str(d["AXDOMIdentifier"])) or None
    return Node(
        ref, ax_role, role, name=name, desc=desc if desc != name else "", value=value, ident=ident, url=url,
        help=_clean(_str(d["AXHelp"])) or None, placeholder=_clean(_str(d["AXPlaceholderValue"])) or None,
        flags=flags, frame=frame,
    )


def _prune(n: Node, depth: int) -> Node | None:
    labels = " ".join(n.texts)
    n.live = n.interactive or any(c.live for c in n.children)
    if labels and not any(c.live for c in n.children):
        # A text leaf that only repeats its parent's label adds nothing (link "Home" > text "Home").
        # Kept when siblings are interactive: then the leaves carry the reading order.
        n.children = [c for c in n.children if not (c.ax_role in TEXT_AX and not c.children and c.name and c.name in labels)]
    if depth == 0:
        return n
    if not n.children and not labels and not n.interactive and n.ax_role != "AXWebArea":
        return None
    if n.ax_role in CONTAINER_AX and not labels and not n.interactive and not n.ident and len(n.children) == 1:
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
