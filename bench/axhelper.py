"""Accessibility helper for benchmark setup and checking (macOS, needs pyobjc).

Runs as a separate process with a pyobjc-capable Python (Voyager's venv by default), so the harness
itself stays stdlib-only and never sends Apple Events (no Automation prompts).

    axhelper.py calc-display                 print Calculator's current display value
    axhelper.py calc-clear                   press All Clear
    axhelper.py calc-press "68.0695"         press digit / operator buttons (self-test only)
    axhelper.py close-windows Finder inbox   close every window of an app whose title contains a string
    axhelper.py windows "Google Chrome"      print the focused window's title, then every window title
"""
from __future__ import annotations

import sys
import time

from AppKit import NSWorkspace
from ApplicationServices import (AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
                                 AXUIElementPerformAction)


def app_el(name: str):
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.localizedName() == name]
    return AXUIElementCreateApplication(apps[0].processIdentifier()) if apps else None


def attr(e, a):
    err, v = AXUIElementCopyAttributeValue(e, a, None)
    return v if err == 0 else None


def walk(e, depth: int = 0):
    yield e
    if depth < 14:
        for c in attr(e, "AXChildren") or []:
            yield from walk(c, depth + 1)


def find(root, pred):
    return next((e for e in walk(root) if pred(e)), None)


def calc_window():
    app = app_el("Calculator")
    if app is None:
        return None
    wins = attr(app, "AXWindows") or []
    return wins[0] if wins else None


def calc_display() -> str | None:
    w = calc_window()
    if w is None:
        return None
    view = find(w, lambda e: attr(e, "AXIdentifier") == "StandardInputView")
    if view is None:
        return None
    txt = find(view, lambda e: attr(e, "AXRole") == "AXStaticText")
    val = attr(txt, "AXValue") if txt is not None else None
    return str(val).replace("‎", "").replace("‏", "").replace(",", "").strip() if val is not None else None


BUTTONS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven",
           "8": "Eight", "9": "Nine", ".": "Decimal", "+": "Add", "-": "Subtract", "*": "Multiply", "/": "Divide",
           "=": "Equals", "C": "AllClear"}


def calc_press(keys: str) -> None:
    w = calc_window()
    for k in keys:
        b = find(w, lambda e, ident=BUTTONS[k]: attr(e, "AXIdentifier") == ident)
        if b is None and k == "C":
            b = find(w, lambda e: attr(e, "AXIdentifier") == "Clear")
        if b is not None:
            AXUIElementPerformAction(b, "AXPress")
            time.sleep(0.05)


def close_windows(app_name: str, needle: str) -> int:
    app = app_el(app_name)
    n = 0
    for w in (attr(app, "AXWindows") or []) if app is not None else []:
        if needle.lower() in str(attr(w, "AXTitle") or "").lower():
            btn = attr(w, "AXCloseButton")
            if btn is not None and AXUIElementPerformAction(btn, "AXPress") == 0:
                n += 1
    return n


def windows(app_name: str) -> list[str]:
    app = app_el(app_name)
    if app is None:
        return []
    focused = attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow")
    titles = [str(attr(w, "AXTitle") or "") for w in attr(app, "AXWindows") or []]
    return [str(attr(focused, "AXTitle") or "") if focused is not None else ""] + titles


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "calc-display":
        print(calc_display() or "")
    elif cmd == "calc-clear":
        calc_press("C")
    elif cmd == "calc-press":
        calc_press(args[0])
    elif cmd == "close-windows":
        print(close_windows(args[0], args[1]))
    elif cmd == "windows":
        print("\n".join(windows(args[0])))
    else:
        sys.exit(f"unknown command {cmd}")
