"""Accessibility state: parse the text tree into elements, stable keys, diff, hash.

The backend prints the bound window's tree as text, one element per line, indented
with tabs::

    Window: "Google", App: Dia.
    0 standard window Google, ID: bigBrowserWindow_..., Secondary Actions: Raise
    \t1 scroll area
    \t\t2 HTML content Google, URL: google.com/webhp?hl=en
    \t\t\t15 text entry area (settable) Search
    \t\t\t21 button Google Search

Indices change between reads, so stable identity comes from role, name, id and ancestry.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

# Longest match first. Every role the backend prints.
KNOWN_ROLES = sorted(
    [
        "standard window", "window", "dialog", "sheet", "popover", "drawer", "application",
        "scroll area", "scroll bar", "HTML content", "web area", "container", "group", "section",
        "link", "button", "pop up button", "menu button", "toggle button", "push button",
        "image", "text entry area", "text area", "text field", "secure text entry area",
        "secure text field", "password text", "password field", "search field", "text",
        "static text", "heading", "paragraph", "collection", "list", "outline", "table", "row",
        "cell", "column", "grid", "unknown", "close button", "minimize button", "full screen button",
        "zoom button", "menu bar", "menu bar item", "menu", "menu item", "check menu item",
        "radio menu item", "check box", "radio button", "radio group", "tab", "tab group",
        "toolbar", "combo box", "slider", "incrementor", "progress indicator", "busy indicator",
        "disclosure triangle", "split group", "splitter", "color well", "date field", "time field",
        "level indicator", "relevance indicator", "help tag", "dock item", "layout area",
        "layout item", "handle", "value indicator", "description list", "term", "definition",
        "mark", "generic", "article", "form", "entry", "label", "frame", "panel", "page tab",
        "page tab list", "tree", "tree item", "list item", "spin button", "status bar",
        "tool tip", "canvas", "document", "video", "audio", "embedded", "separator",
    ],
    key=len,
    reverse=True,
)
_ROLE_RE = re.compile("|".join(re.escape(r) for r in KNOWN_ROLES))
_ATTR_KEYS = ("Value", "ID", "URL", "Help", "Secondary Actions", "Placeholder", "Description", "Title")
_ATTR_RE = re.compile(r"(?:^|, )(" + "|".join(re.escape(k) for k in _ATTR_KEYS) + r"): ")
_LINE_RE = re.compile(r"^(\t*)(\d+) (.*)$")
_INVISIBLE = re.compile("[\u200e\u200f\u200b\u2060\ufeff]")
_HEADER_RE = re.compile(r'^Window: "(.*)", App: (.*?)\.?$')
_FOCUS_RE = re.compile(r"^The focused UI element is (\d+) ")

INTERACTIVE_ROLES = {
    "link", "button", "pop up button", "menu button", "toggle button", "push button",
    "text entry area", "text area", "text field", "secure text entry area", "secure text field",
    "password text", "password field", "search field", "check box", "radio button", "tab",
    "menu item", "check menu item", "radio menu item", "menu bar item", "combo box", "slider",
    "incrementor", "disclosure triangle", "close button", "minimize button", "full screen button",
    "zoom button", "spin button", "page tab", "tree item", "entry", "list item", "cell", "row",
}
TEXT_ROLES = {"text", "static text", "heading", "paragraph", "label", "term", "definition", "tool tip"}
TEXT_INPUT_ROLES = {
    "text entry area", "text area", "text field", "secure text entry area", "secure text field",
    "password text", "password field", "search field", "combo box", "entry",
}
PASSWORD_ROLES = {"secure text entry area", "secure text field", "password text", "password field"}
CONTAINER_ROLES = {
    "scroll area", "HTML content", "web area", "container", "group", "section", "collection",
    "list", "outline", "table", "dialog", "sheet", "popover", "article", "form", "panel",
    "tab group", "document", "frame", "standard window", "window",
}
_PASSWORD_HINT = re.compile(
    r"password|passcode|passwd|\bpin\b|one[- ]?time|\botp\b|verification code|security code|2fa|mfa|cvv|cvc",
    re.I,
)


@dataclass
class Element:
    index: int
    role: str
    name: str = ""
    value: str | None = None
    id: str | None = None
    url: str | None = None
    help: str | None = None
    actions: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    depth: int = 0
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    raw: str = ""
    key: str = ""
    bounds: tuple[float, float, float, float] | None = None  # screen points (native reader only)

    @property
    def disabled(self) -> bool:
        return "disabled" in self.flags

    @property
    def settable(self) -> bool:
        return "settable" in self.flags

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES or bool(self.actions) or self.settable or (
            self.role == "unknown" and bool(self.id or self.name)
        )

    @property
    def is_text_input(self) -> bool:
        return self.role in TEXT_INPUT_ROLES or (self.settable and self.role not in {"scroll bar", "slider"})

    @property
    def password_marker(self) -> bool:
        """True when the AX state itself marks this as a secure field."""
        return self.role in PASSWORD_ROLES or "secure" in self.flags or "password" in self.flags

    @property
    def password_hint(self) -> bool:
        """Soft hint from the name, id or placeholder. Confirmed by a Noul when there is no marker."""
        return bool(_PASSWORD_HINT.search(f"{self.name} {self.id or ''} {self.help or ''}"))

    @property
    def label(self) -> str:
        return self.name or self.help or self.id or (self.value[:40] if self.value else "") or ""


@dataclass
class AXTree:
    window_title: str = ""
    app: str = ""
    elements: list[Element] = field(default_factory=list)
    focused: int | None = None
    raw: str = ""
    selected_text: str | None = None

    def __post_init__(self) -> None:
        self.by_index: dict[int, Element] = {e.index: e for e in self.elements}
        self.by_key: dict[str, Element] = {}
        _assign_keys(self)
        self.hash = compute_hash(self)

    def get(self, index: int) -> Element | None:
        return self.by_index.get(index)

    def find_key(self, key: str) -> Element | None:
        return self.by_key.get(key)

    def ancestors(self, el: Element) -> list[Element]:
        out: list[Element] = []
        cur = el
        while cur.parent is not None:
            cur = self.by_index[cur.parent]
            out.append(cur)
        return out

    def named_ancestor(self, el: Element) -> Element | None:
        for a in self.ancestors(el):
            if a.name and a.role not in {"standard window", "window", "menu bar"}:
                return a
        return None

    def subtree(self, el: Element) -> Iterable[Element]:
        stack = [el]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(reversed([self.by_index[c] for c in cur.children]))

    @property
    def url(self) -> str | None:
        """Best-effort page URL. Chrome sometimes elides the content node's URL to "…", so every
        candidate is validated; order: web content URL, address bar value, any other URL attribute."""
        content = [e.url for e in self.elements if e.url and e.role in ("HTML content", "web area")]
        bars = []
        for e in self.elements:
            if e.is_text_input and e.value:
                ident = f"{e.id or ''} {e.name}".lower()
                if any(k in ident for k in ("address", "omnibox", "navigationbar", "commandbar", "url", "location")):
                    bars.append(e.value.split(" / ")[0].strip())
        others = [e.url for e in self.elements if e.url]
        for cand in [*content, *bars, *others]:
            if usable_url(cand):
                return cand
        return None

    @property
    def host(self) -> str | None:
        return host_of(self.url)

    def visible_text(self, limit: int = 8000) -> str:
        parts: list[str] = []
        total = 0
        for e in self.elements:
            if e.role in TEXT_ROLES or (e.role in CONTAINER_ROLES and e.name) or e.role == "link" or e.role == "heading":
                t = e.name
            elif e.is_text_input and e.value:
                t = f"{e.name}: {e.value}" if e.name else e.value
            else:
                continue
            t = t.strip()
            if not t:
                continue
            parts.append(t)
            total += len(t) + 1
            if total > limit:
                parts.append("…")
                break
        return "\n".join(parts)

    def interactive(self) -> list[Element]:
        return [e for e in self.elements if e.interactive and not e.disabled]

    def containers(self) -> list[Element]:
        return [e for e in self.elements if e.role in CONTAINER_ROLES and e.children]


_ELLIPSIS = "…"


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    u = url if "://" in url else "https://" + url
    try:
        host = (urlsplit(u).hostname or "").lower()
    except ValueError:
        return None
    if not host or _ELLIPSIS in host or "..." in host:
        return None
    return host


def usable_url(s: str | None) -> bool:
    """A URL whose host is complete: not elided ("…"), has a dot (or is localhost), no spaces."""
    if not s or " " in s.strip():
        return False
    host = host_of(s)
    if not host:
        return False
    return host == "localhost" or ("." in host and not host.endswith("."))


def _split_attrs(rest: str) -> tuple[str, dict[str, str]]:
    """Split 'Name, Value: v, ID: x' into the name and attribute dict. Names may contain commas."""
    attrs: dict[str, str] = {}
    matches = list(_ATTR_RE.finditer(rest))
    if not matches:
        return rest.strip(), attrs
    name = rest[: matches[0].start()].strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        attrs[m.group(1)] = rest[m.end() : end].strip()
    return name, attrs


def parse_line(line: str) -> tuple[int, int, Element] | None:
    m = _LINE_RE.match(line)
    if not m:
        return None
    depth = len(m.group(1))
    index = int(m.group(2))
    body = _INVISIBLE.sub("", m.group(3))
    role_m = _ROLE_RE.match(body)
    if role_m and (len(body) == role_m.end() or body[role_m.end()] in " ,("):
        role = role_m.group(0)
        rest = body[role_m.end() :].lstrip()
    else:
        role = "menu bar item" if depth >= 1 and body[:1].isupper() else "unknown"
        rest = body
    flags: set[str] = set()
    fm = re.match(r"\(([^)]*)\)\s*", rest)
    if fm:
        flags = {f.strip() for f in fm.group(1).split(",") if f.strip()}
        rest = rest[fm.end() :]
    name, attrs = _split_attrs(rest)
    actions = [a.strip() for a in attrs.get("Secondary Actions", "").split(",") if a.strip()]
    el = Element(
        index=index,
        role=role,
        name=name,
        value=attrs.get("Value"),
        id=attrs.get("ID"),
        url=attrs.get("URL"),
        help=attrs.get("Help") or attrs.get("Description") or attrs.get("Placeholder"),
        actions=actions,
        flags=flags,
        depth=depth,
        raw=line,
    )
    return depth, index, el


def parse_ax(text: str) -> AXTree:
    tree = AXTree(raw=text)
    elements: list[Element] = []
    stack: list[tuple[int, int]] = []  # (depth, index)
    lines = text.split("\n")
    in_selection = False
    selection: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if in_selection:
            if line.startswith("```"):
                in_selection = False
            else:
                selection.append(line)
            continue
        hm = _HEADER_RE.match(line)
        if hm:
            tree.window_title, tree.app = hm.group(1), hm.group(2)
            continue
        fm = _FOCUS_RE.match(line)
        if fm:
            tree.focused = int(fm.group(1))
            continue
        if line.startswith("Selected text:"):
            in_selection = True
            continue
        parsed = parse_line(line)
        if not parsed:
            continue
        depth, index, el = parsed
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            el.parent = stack[-1][1]
        elements.append(el)
        stack.append((depth, index))
    by_index = {e.index: e for e in elements}
    for e in elements:
        if e.parent is not None and e.parent in by_index:
            by_index[e.parent].children.append(e.index)
    tree.elements = elements
    tree.selected_text = "\n".join(selection) if selection else None
    tree.__post_init__()
    return tree


def _assign_keys(tree: AXTree) -> None:
    """Stable key: role | name | id | two named ancestors | ordinal among identical siblings."""
    by_index = {e.index: e for e in tree.elements}
    counts: dict[str, int] = {}
    for e in tree.elements:
        chain: list[str] = []
        cur = e
        while cur.parent is not None and len(chain) < 2:
            cur = by_index[cur.parent]
            if cur.name or cur.id:
                chain.append(f"{cur.role}:{cur.name or cur.id}")
        base = f"{e.role}|{e.name}|{e.id or ''}|{'>'.join(chain)}"
        n = counts.get(base, 0)
        counts[base] = n + 1
        e.key = base if n == 0 else f"{base}#{n}"
    tree.by_key = {e.key: e for e in tree.elements}


def compute_hash(tree: AXTree) -> str:
    h = hashlib.sha1()
    for e in tree.elements:
        if e.interactive or e.role in TEXT_ROLES or e.name:
            h.update(f"{e.role}|{e.name}|{e.value or ''}|{','.join(sorted(e.flags))}\n".encode())
    h.update(tree.window_title.encode())
    return h.hexdigest()


@dataclass
class Diff:
    added: list[Element]
    removed: list[Element]
    changed: list[tuple[Element, Element]]  # (old, new)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self, limit: int = 3000) -> str:
        lines: list[str] = []
        named = lambda e: bool(e.name or e.value or e.id or e.help)  # noqa: E731
        for e in self.added:
            if named(e):
                lines.append(f"+ {describe(e)}")
        for e in self.removed:
            if named(e):
                lines.append(f"- {describe(e)}")
        for old, new in self.changed:
            lines.append(f"~ {describe(new)} (was value: {old.value!r})")
        s = "\n".join(lines)
        return s if len(s) <= limit else s[:limit] + "\n…"


def diff(old: AXTree, new: AXTree) -> Diff:
    added = [e for e in new.elements if e.key not in old.by_key]
    removed = [e for e in old.elements if e.key not in new.by_key]
    changed = []
    for e in new.elements:
        o = old.by_key.get(e.key)
        if o and (o.value != e.value or o.flags != e.flags):
            changed.append((o, e))
    return Diff(added, removed, changed)


def describe(e: Element, tree: AXTree | None = None) -> str:
    """Short, literal description for an Eyes question, e.g. button "Chat with us" · in "Help"."""
    parts = [e.role]
    if e.name:
        parts.append(f'"{e.name[:80]}"')
    elif e.help:  # macOS often exposes the label as Description for icon buttons
        parts.append(f'"{e.help[:80]}"')
    elif e.id:
        parts.append(f"id {e.id[:60]}")
    if e.id and (e.name or e.help) and e.id.lower() != (e.name or e.help).lower():
        parts.append(f"(id {e.id[:40]})")
    if e.value and e.role != "link":
        parts.append(f"value {e.value[:60]!r}")
    if e.settable:
        parts.append("(editable)")
    if e.disabled:
        parts.append("(disabled)")
    if tree is not None:
        anc = tree.named_ancestor(e)
        if anc:
            parts.append(f'· in {anc.role} "{anc.name[:40]}"')
    return " ".join(parts)


def candidates(tree: AXTree, limit: int = 254) -> list[Element]:
    """Visible, enabled, interactive elements in document order, at most ``limit``."""
    out = [e for e in tree.elements if e.interactive and not e.disabled and e.role != "scroll bar"]
    # Prefer named/identified elements when trimming.
    if len(out) > limit:
        out = sorted(out, key=lambda e: (not (e.name or e.id), e.index))[:limit]
        out.sort(key=lambda e: e.index)
    return out


_SCOPE_STOP_ROLES = {"HTML content", "standard window", "window", "scroll area", "web area", "application"}


def item_scope(tree: AXTree, el: Element, max_climb: int = 6) -> Element:
    """The largest ancestor that still holds only this one control of its kind: the row, card or list
    item the element belongs to. Stops before a container holding a sibling control of the same role."""
    scope = el
    for anc in tree.ancestors(el)[:max_climb]:
        if anc.role in _SCOPE_STOP_ROLES:
            break
        if any(e is not el and e.role == el.role for e in tree.subtree(anc)):
            break
        scope = anc
    return scope


def scope_text(tree: AXTree, el: Element, limit: int = 100) -> str:
    """The other text in the element's row or card, e.g. 'USB-C cable 1 m, braided | CB-102 | $13.00'."""
    scope = item_scope(tree, el)
    if scope is el:
        return ""
    seen: list[str] = []
    for e in tree.subtree(scope):
        t = (e.name or e.value or "").strip()
        if e is not el and t and t not in seen and t != el.name:
            seen.append(t)
    s = " | ".join(seen)
    return s if len(s) <= limit else s[:limit] + "…"


def candidate_criteria(tree: AXTree, cands: list[Element]) -> tuple[dict[str, str], dict[str, Element]]:
    """Criteria for a Choice question plus the label -> element mapping. Adds 'none' last.
    Candidates that would read the same (every row's "Remove" button) also show their row's text."""
    crit: dict[str, str] = {}
    mapping: dict[str, Element] = {}
    descs = [describe(e, tree) for e in cands]
    counts: dict[str, int] = {}
    for d in descs:
        counts[d] = counts.get(d, 0) + 1
    for i, (e, d) in enumerate(zip(cands, descs)):
        label = f"e{i}"
        if counts[d] > 1 and (row := scope_text(tree, e)):
            d = f'{d} · row "{row}"'
        crit[label] = d
        mapping[label] = e
    crit["none"] = "none of the listed elements is the target"
    return crit, mapping
