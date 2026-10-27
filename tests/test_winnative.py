"""Windows UI Automation backend pieces that run without a live app (chords, role mapping, the
reader over fake elements)."""
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Windows backend", allow_module_level=True)
pytest.importorskip("comtypes")
pytest.importorskip("win32gui")

from voyager.ax import parse_ax  # noqa: E402
from voyager.backend import winuia  # noqa: E402
from voyager.backend.base import RuntimeError_  # noqa: E402
from voyager.backend.winnative import VK, VK_CONTROL, VK_LWIN, VK_SHIFT, WinNativeBackend, parse_chord  # noqa: E402
from voyager.backend.winuia import Snapshot, U, UIAReader  # noqa: E402

CT = {v: k for k, v in winuia.CT.items()}


def test_parse_chord():
    assert parse_chord("Return") == (0x0D, [])
    assert parse_chord("Tab") == (0x09, [])
    assert parse_chord("ctrl+a") == (ord("A"), [VK_CONTROL])
    assert parse_chord("cmd+a") == (ord("A"), [VK_CONTROL])  # macOS-style plans keep working
    assert parse_chord("super+shift+Left") == (0x25, [VK_LWIN, VK_SHIFT])
    assert parse_chord("A") == (ord("A"), [VK_SHIFT])
    assert parse_chord("KP_0")[0] == 0x60
    assert parse_chord("page_down")[0] == VK["page_down"]
    with pytest.raises(ValueError):
        parse_chord("hyper+x")
    with pytest.raises(ValueError):
        parse_chord("ctrl+nosuchkey")


class FakeArr:
    def __init__(self, items):
        self.items = items
        self.Length = len(items)

    def GetElement(self, i):
        return self.items[i]


class FakeEl:
    def __init__(self, ctype, name="", children=(), frame=(0, 0, 100, 20), **props):
        self.props = {U.UIA_ControlTypePropertyId: CT[ctype], U.UIA_NamePropertyId: name,
                      U.UIA_BoundingRectanglePropertyId: frame, U.UIA_IsEnabledPropertyId: True}
        for k, v in props.items():
            self.props[getattr(U, f"UIA_{k}PropertyId")] = v
        self.children = list(children)

    def GetCachedPropertyValue(self, pid):
        return self.props.get(pid, object())  # object(): "not supported"

    def GetCachedChildren(self):
        return FakeArr(self.children) if self.children else None


class FakeUIA:
    def __init__(self, roots):
        self.roots = roots

    def ElementFromHandleBuildCache(self, hwnd, cache):
        return self.roots[hwnd]


def reader(roots, popups=()):
    r = UIAReader.__new__(UIAReader)
    r.window_fn = lambda: (1, list(popups))
    r.app_name = "Fake"
    r.margin = 1.0
    r.uia = FakeUIA(roots)
    r._cache = {"full": None, "action": None}
    r.hwnd = r.window = None
    r._visited, r._cap = 0, 6000
    return r


def page():
    link = FakeEl("Hyperlink", "Home", children=[FakeEl("Text", "Home")], IsInvokePatternAvailable=True,
                  IsValuePatternAvailable=True, ValueValue="https://shop.test/")
    name = FakeEl("Edit", "Your name", IsValuePatternAvailable=True, ValueIsReadOnly=False, ValueValue="Ada",
                  HasKeyboardFocus=True, AutomationId="nameBox")
    pw = FakeEl("Edit", "Password", IsPassword=True, IsValuePatternAvailable=True, ValueIsReadOnly=False, ValueValue="")
    check = FakeEl("CheckBox", "Subscribe", IsTogglePatternAvailable=True, ToggleToggleState=1)
    heading = FakeEl("Text", "Your cart", LocalizedControlType="heading")
    off = FakeEl("Button", "Far away", frame=(0, 5000, 100, 20), IsInvokePatternAvailable=True)
    disabled = FakeEl("Button", "Pay", IsEnabled=False)
    doc = FakeEl("Document", "Shop", FrameworkId="Chrome", IsValuePatternAvailable=True, ValueIsReadOnly=True,
                 ValueValue="https://shop.test/cart", IsScrollPatternAvailable=True,
                 children=[heading, link, FakeEl("Group", children=[name]), pw, check, off, disabled, FakeEl("Group")])
    win = FakeEl("Window", "Shop - Edge", frame=(0, 0, 1000, 800), children=[FakeEl("Pane", children=[doc])])
    return win


def test_reader_prints_the_parser_format():
    snap = reader({1: page()}).read("action")
    tree = parse_ax(snap.text)
    assert tree.window_title == "Shop - Edge" and tree.app == "Fake"
    roles = {e.name or e.value: e.role for e in tree.elements}
    assert roles["Shop"] == "HTML content"
    assert tree.url == "https://shop.test/cart" and tree.host == "shop.test"
    link = next(e for e in tree.elements if e.role == "link")
    assert link.name == "Home" and link.value == "https://shop.test/" and not link.children  # repeated text dropped
    name = next(e for e in tree.elements if e.name == "Your name")
    assert name.role == "text field" and name.value == "Ada" and name.settable and name.id == "nameBox"
    assert tree.focused == name.index
    pw = next(e for e in tree.elements if e.name == "Password")
    assert pw.role == "secure text field" and pw.password_marker
    assert next(e for e in tree.elements if e.name == "Subscribe").flags == {"checked"}
    assert next(e for e in tree.elements if e.name == "Your cart").role == "heading"
    assert next(e for e in tree.elements if e.name == "Pay").disabled
    # The action scope drops what is far outside the window; the full scope keeps it.
    assert not any(e.name == "Far away" for e in tree.elements) and snap.partial
    full = parse_ax(reader({1: page()}).read("full").text)
    assert any(e.name == "Far away" for e in full.elements)
    # Unnamed single-child wrappers collapse; empty groups are dropped.
    assert all(not (e.role == "container" and not e.children) for e in tree.elements)


def test_reader_keeps_refs_frames_and_actions():
    snap = reader({1: page()}).read("action")
    tree = parse_ax(snap.text)
    link = next(e for e in tree.elements if e.role == "link")
    assert "invoke" in snap.actions[link.index]
    assert snap.frames[link.index] == (0.0, 0.0, 100.0, 20.0)
    name = next(e for e in tree.elements if e.name == "Your name")
    assert name.index in snap.text_inputs
    doc = next(e for e in tree.elements if e.role == "HTML content")
    assert snap.ctypes[doc.index] == "Document" and "scroll" in snap.actions[doc.index]
    assert snap.parents[name.index] == doc.index
    assert set(snap.refs) == {e.index for e in tree.elements}


def test_reader_reads_popups_and_dialogs():
    dialog = FakeEl("Window", "Save changes?", IsDialog=True,
                    children=[FakeEl("Button", "Save", IsInvokePatternAvailable=True), FakeEl("Button", "Don't save")])
    snap = reader({1: page(), 2: dialog}, popups=[2]).read("action")
    tree = parse_ax(snap.text)
    d = next(e for e in tree.elements if e.name == "Save changes?")
    assert d.role == "dialog" and d.parent is None
    assert [tree.get(c).name for c in d.children] == ["Save", "Don't save"]


def test_reader_without_window_raises_lookup_error():
    r = reader({})
    r.window_fn = lambda: (None, [])
    with pytest.raises(LookupError):
        r.read()


def test_non_web_document_is_a_text_area():
    doc = FakeEl("Document", "Text editor", FrameworkId="Win32", IsValuePatternAvailable=True, ValueIsReadOnly=False, ValueValue="hello")
    tree = parse_ax(reader({1: FakeEl("Window", "Untitled - Notepad", children=[doc])}).read().text)
    el = next(e for e in tree.elements if e.name == "Text editor")
    assert el.role == "text entry area" and el.value == "hello" and el.is_text_input


def test_pid_alive():
    import os

    from voyager.proc import pid_alive

    assert pid_alive(os.getpid())
    assert not pid_alive(999999)
    assert not pid_alive(0)


# ---- action strategy over a fake snapshot ----------------------------------------------------------


class FakePattern:
    def __init__(self, log, name):
        self.log, self.name = log, name
        self.CurrentExpandCollapseState = 0

    def QueryInterface(self, iface):
        return self

    def __getattr__(self, attr):  # Invoke, Toggle, Select, Expand, Collapse
        return lambda *a: self.log.append(f"{self.name}.{attr}")


class FakeRef:
    def __init__(self, log):
        self.log = log

    def GetCurrentPattern(self, pid):
        return FakePattern(self.log, {U.UIA_InvokePatternId: "invoke", U.UIA_TogglePatternId: "toggle",
                                      U.UIA_SelectionItemPatternId: "select", U.UIA_ExpandCollapsePatternId: "expand"}[pid])

    def SetFocus(self):
        self.log.append("focus")


def backend_with(actions: set[str], framework="Win32", text_input=False):
    b = WinNativeBackend.__new__(WinNativeBackend)
    log: list[str] = []
    b._snap = Snapshot("", {0: FakeRef(log)}, {}, None, 1, 0.0, False, actions={0: actions},
                       ctypes={0: "ListItem"}, frameworks={0: framework}, text_inputs={0} if text_input else set())
    b.app_name, b.hwnd, b.pids = "Fake", 1, {1}
    b._center = lambda ref: (10.0, 10.0)
    b._activate = lambda: log.append("activate")
    b._hit_is = lambda ref, x, y: True
    b._mouse_click = lambda x, y, count=1, button="left": log.append(f"mouse{count}{button}")
    return b, log


def test_click_prefers_patterns_and_falls_back_to_a_real_click():
    b, log = backend_with({"invoke", "select"})
    assert b._do_click(0, None, 1, "left", False) == "uia_invoke" and log == ["invoke.Invoke"]
    b, log = backend_with({"toggle"})
    assert b._do_click(0, None, 1, "left", False) == "uia_toggle"
    b, log = backend_with(set(), text_input=True)
    assert b._do_click(0, None, 1, "left", False) == "uia_focus" and log == ["focus"]
    # Select raises the app's events only in XAML/WPF; a Win32 or Chromium list item gets a real click.
    b, log = backend_with({"select"}, framework="XAML")
    assert b._do_click(0, None, 1, "left", False) == "uia_select"
    b, log = backend_with({"select"}, framework="Win32")
    assert b._do_click(0, None, 1, "left", False) == "mouse" and log[-1] == "mouse1left"
    # Retries, double and right clicks always use the pointer.
    b, log = backend_with({"invoke"})
    assert b._do_click(0, None, 1, "left", True) == "mouse"
    b, log = backend_with({"invoke"})
    assert b._do_click(0, None, 2, "left", False) == "mouse" and log[-1] == "mouse2left"
    b, log = backend_with({"invoke"})
    assert b._do_click(0, None, 1, "right", False) == "mouse" and log[-1] == "mouse1right"


def test_expand_toggles_open_and_closed():
    b, log = backend_with({"expand"})
    assert b._do_click(0, None, 1, "left", False) == "uia_expand" and log == ["expand.Expand"]


def test_pointer_click_refuses_when_another_window_covers_the_point():
    b, log = backend_with(set())
    del b._mouse_click
    b._activate = lambda: None
    b._owns_point = lambda x, y: False
    with pytest.raises(RuntimeError_, match="another window covers"):
        WinNativeBackend._mouse_click(b, 10, 10)


def test_scroll_owner_climbs_to_the_nearest_scrollable_ancestor():
    b = WinNativeBackend.__new__(WinNativeBackend)
    refs = {0: "win", 1: "doc", 2: "list", 3: "item"}
    b._snap = Snapshot("", refs, {}, None, 4, 0.0, False, actions={1: {"scroll"}, 2: {"scroll"}, 3: set()},
                       ctypes={0: "Window", 1: "Document", 2: "List", 3: "ListItem"}, parents={1: 0, 2: 1, 3: 2})
    assert b._scroll_owner(3) == "list"
    assert b._scroll_owner(None) == "doc"  # the page: the web document
    b._snap.actions = {2: {"scroll"}}
    b._snap.ctypes[1] = "Pane"
    assert b._scroll_owner(None) == "list"  # no document: the first scrollable element


def test_a_pattern_that_refuses_falls_back_to_a_real_click():
    import comtypes

    b, log = backend_with({"invoke"})

    class Refusing(FakePattern):
        def Invoke(self):
            raise comtypes.COMError(-2146233079, None, (None, None, None, 0, None))  # InvalidOperation

    b._snap.refs[0].GetCurrentPattern = lambda pid: Refusing(log, "invoke")
    assert b._do_click(0, None, 1, "left", False) == "mouse" and log[-1] == "mouse1left"


def test_web_list_items_are_targets_only_with_an_action_of_their_own():
    link = FakeEl("Hyperlink", "Privacy policy", IsInvokePatternAvailable=True, IsValuePatternAvailable=True, ValueValue="https://x.test/p")
    wrapper = FakeEl("ListItem", "Privacy policy", FrameworkId="Chrome", children=[link])
    clickable = FakeEl("ListItem", "Chat with Dad", FrameworkId="Chrome", IsInvokePatternAvailable=True)
    native = FakeEl("ListItem", "Fruit item 3", FrameworkId="Win32")
    doc = FakeEl("Document", "Page", FrameworkId="Chrome", children=[FakeEl("List", children=[wrapper, clickable]), native])
    tree = parse_ax(reader({1: FakeEl("Window", "W", children=[doc])}).read().text)
    named = [e for e in tree.elements if e.name == "Privacy policy"]
    assert [e.role for e in named] == ["container", "link"]
    assert [e.name for e in tree.interactive() if "Privacy" in e.name] == ["Privacy policy"]  # the link only
    assert next(e for e in tree.elements if e.name == "Chat with Dad").role == "list item"
    assert next(e for e in tree.elements if e.name == "Fruit item 3").role == "list item"
