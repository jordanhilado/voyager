"""Deterministic guards that stop a batched plan where it would otherwise go wrong unseen.

- ``identity_mismatch``: the details a target names (quoted labels, model numbers, IDs, emails, times)
  must appear on the picked element or around it (its ancestors' names and its own row or card).
  Catches near-identical picks such as "Aurora 32U" for "Aurora 27U", or the "Remove" button in the
  wrong row, whatever the step's ``expect`` says.
- ``is_destructive``: clicks that delete, remove, buy or pay get the shuffled double check that
  commits get.
- ``meaningful_change``: whether an action visibly changed the page. A step whose check failed is
  retried only when nothing changed, so a retry can never perform the action twice.
"""
from __future__ import annotations

import re

from .ax import AXTree, Diff, Element, item_scope

# A quote that opens a quoted label: not preceded by a letter or digit, so "Lena's" and "O'Neil" are not quotes.
_QUOTED = re.compile(r"(?<![A-Za-z0-9])[\"'“‘]([^\"'”’]{1,80})[\"'”’](?![A-Za-z0-9])")
_WORD = re.compile(r"[a-z0-9]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN = re.compile(r"[A-Za-z0-9$][A-Za-z0-9.\-_/:$]*")
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th)$")
# Words that describe the control rather than name the item; never required.
_GENERIC = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "with", "by", "from", "its", "this", "that",
    "button", "link", "tab", "field", "box", "input", "menu", "item", "option", "row", "column", "cell", "list",
    "icon", "checkbox", "radio", "dropdown", "select", "panel", "dialog", "page", "section", "area", "label",
}
DESTRUCTIVE = re.compile(
    r"\b(delete|remove|discard|erase|trash|archive|unsubscribe|revoke|deactivate|uninstall|reset|clear all|"
    r"place order|buy now|purchase|pay now|pay|checkout|cancel (?:order|subscription|account|booking|reservation|plan))\b",
    re.I,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9@]", "", s.lower())


def distinguishing_tokens(target: str) -> list[str]:
    """What the target names that must show up on the pick: emails, identifier-like tokens with a
    digit (27U, PR-103, 15:00, $45), and the significant words of quoted labels."""
    target = target or ""
    out: list[str] = [m.group(0) for m in _EMAIL.finditer(target)]
    rest = _EMAIL.sub(" ", target)
    for t in _TOKEN.findall(rest):
        t = t.strip(".:/-_")
        if any(c.isdigit() for c in t) and not _ORDINAL.match(t.lower()):
            out.append(t)
    for phrase in _QUOTED.findall(rest):
        for w in _WORD.findall(phrase.lower()):
            if len(w) >= 2 and w not in _GENERIC and not any(c.isdigit() for c in w):
                out.append(w)
    seen: set[str] = set()
    return [t for t in out if not (t.lower() in seen or seen.add(t.lower()))]


def element_context(tree: AXTree, el: Element, limit: int = 4000) -> str:
    parts = [el.name, el.help or "", el.id or "", el.value or ""]
    parts += [a.name for a in tree.ancestors(el) if a.name]
    scope = item_scope(tree, el)
    if scope is not el:
        parts += [f"{e.name} {e.value or ''}" for e in tree.subtree(scope)]
    return " ".join(p for p in parts if p)[:limit]


_NUMBER = re.compile(r"^\d+(?:[.,]\d+)*$")


def _number_present(num: str, ctx_lower: str) -> bool:
    """A whole number, not part of a longer one: "2" is not in "2.1" or "HD-212"; "45" is in "$45.00"."""
    num = num.replace(",", "")
    ctx = ctx_lower.replace(",", "")
    alts = {num, num.rstrip("0").rstrip(".")} if "." in num else {num}
    return any(re.search(rf"(?<![\w.]){re.escape(a)}(?:\.0+)?(?!\w|\.\d)", ctx) for a in alts if a)


def _token_present(token: str, ctx_norm: str, ctx_lower: str) -> bool:
    if "@" in token:
        return token.lower() in ctx_lower
    bare = token.lstrip("$")
    if _NUMBER.match(bare):
        return _number_present(bare, ctx_lower)
    if _norm(token) in ctx_norm:
        return True
    # "27-inch" matches a page that shows 27": every run that carries a digit must be there.
    runs = [r for r in _WORD.findall(token.lower()) if any(c.isdigit() for c in r)]
    return bool(runs) and all(r in ctx_norm for r in runs)


def identity_mismatch(target: str, tree: AXTree, el: Element | None) -> list[str]:
    """Tokens the target names that the picked element and its row/card do not show. Empty = consistent."""
    if el is None:
        return []
    tokens = distinguishing_tokens(target)
    if not tokens:
        return []
    ctx = element_context(tree, el)
    ctx_norm, ctx_lower = _norm(ctx), ctx.lower()
    return [t for t in tokens if not _token_present(t, ctx_norm, ctx_lower)]


def is_destructive(*texts: str | None) -> bool:
    return any(t and DESTRUCTIVE.search(t) for t in texts)


def meaningful_change(d: Diff | None) -> bool:
    """True when the action visibly did something: a named element appeared or went away, or an
    element's value or state (checked, selected, expanded, disabled) changed."""
    if d is None:
        return False
    named = lambda e: bool(e.name or e.value or e.id or e.help)  # noqa: E731
    return bool(d.changed) or any(named(e) for e in d.added) or any(named(e) for e in d.removed)


def change_summary(d: Diff | None, limit: int = 240) -> str:
    if d is None or d.empty:
        return "no visible change"
    s = d.summary(limit + 40).replace("\n", "; ")
    return s if len(s) <= limit else s[:limit] + "…"
