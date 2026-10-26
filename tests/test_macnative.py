"""Native macOS backend pieces that run without a live app (chords, formatting, pruning)."""
import pytest

pytest.importorskip("ApplicationServices")
Quartz = pytest.importorskip("Quartz")

from voyager.ax import parse_ax  # noqa: E402
from voyager.backend.macax import Node, _format, _prune  # noqa: E402
from voyager.backend.macnative import KEYCODES, parse_chord  # noqa: E402


def test_parse_chord():
    assert parse_chord("Return") == (36, 0)
    assert parse_chord("Tab") == (48, 0)
    code, flags = parse_chord("ctrl+a")
    assert code == KEYCODES["a"] and flags == Quartz.kCGEventFlagMaskControl
    code, flags = parse_chord("super+shift+Left")
    assert code == 123 and flags == Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
    assert parse_chord("A")[1] == Quartz.kCGEventFlagMaskShift
    assert parse_chord("KP_0")[0] == 82
    with pytest.raises(ValueError):
        parse_chord("hyper+x")


def _n(ax_role, role, **kw):
    children = kw.pop("children", [])
    n = Node(None, ax_role, role, **kw)
    for c in children:
        n.children.append(c)
    return n


def test_prune_collapses_and_drops_redundant_nodes():
    text = _n("AXStaticText", "text", name="Home")
    link = _prune(_n("AXLink", "link", name="Home", value="https://x.test/", children=[text]), 3)
    assert link.children == []  # the text leaf only repeated the link's label
    wrapper = _prune(_n("AXGroup", "container", children=[link]), 2)
    assert wrapper is link  # unnamed single-child container collapses
    assert _prune(_n("AXGroup", "container"), 2) is None  # empty container dropped
    # Text leaves stay when siblings are interactive (they carry the reading order).
    body = _n("AXStaticText", "text", name="hello")
    comment = _prune(_n("AXGroup", "container", name="bob hello reply", children=[_n("AXStaticText", "text", name="bob"), body, link]), 2)
    assert body in comment.children


def test_format_round_trips_through_parse_ax():
    lines = [
        "0 " + _format(_n("AXWindow", "standard window", name="Chat")),
        "\t1 " + _format(_n("AXTextArea", "text entry area", name="", placeholder="Message", flags=["settable"])),
        "\t2 " + _format(_n("AXButton", "button", name="", desc="Send")),
        "\t3 " + _format(_n("AXTextField", "secure text field", name="Password", flags=["settable"])),
        "\t4 " + _format(_n("AXLink", "link", name="Docs", value="https://x.test/docs", ident="docs")),
    ]
    tree = parse_ax('Window: "Chat", App: Google Chrome.\n' + "\n".join(lines))
    box, send, pw, link = (tree.get(i) for i in (1, 2, 3, 4))
    assert box.role == "text entry area" and box.settable and box.help == "Message"
    assert send.role == "button" and send.label == "Send"
    assert pw.password_marker
    assert link.value == "https://x.test/docs" and link.id == "docs"


def test_match_window_by_frame():
    from voyager.backend.macwin import match_window

    wins = [
        {"id": 1, "pid": 9, "frame": (0.0, 25.0, 800.0, 600.0), "on_screen": False},
        {"id": 2, "pid": 9, "frame": (0.0, 25.0, 800.0, 600.0), "on_screen": True},
        {"id": 3, "pid": 9, "frame": (900.0, 25.0, 400.0, 300.0), "on_screen": True},
    ]
    assert match_window((0.0, 25.0, 800.0, 600.0), wins) == 2  # on-screen wins a tie
    assert match_window((901.0, 27.0, 399.0, 300.0), wins) == 3  # within tolerance
    assert match_window((0.0, 25.0, 700.0, 600.0), wins) is None
    assert match_window(None, wins) is None


async def test_observe_full_without_screen_recording_is_text_only(monkeypatch):
    from voyager.backend import macnative, macwin
    from voyager.backend.macax import Snapshot
    from voyager.config import Settings

    b = macnative.MacNativeBackend(Settings())
    snap = Snapshot(text='Window: "W", App: X.\n0 standard window W', refs={}, frames={}, window_frame=(0, 0, 100, 100), nodes=1, ms=1.0, partial=False)

    class Reader:
        window = object()

        def read(self, scope):
            return snap

        def frame_of(self, ref):
            return (0.0, 0.0, 100.0, 100.0)

    b.reader, b.pid, b.app_name = Reader(), 9, "X"
    monkeypatch.setattr(macwin, "cg_windows", lambda pid=None, on_screen_only=False: [{"id": 7, "pid": 9, "frame": (0.0, 0.0, 100.0, 100.0), "on_screen": True}])
    monkeypatch.setattr(macwin, "screen_recording_granted", lambda: False)
    obs = await b.observe_full()
    assert obs.screenshot is None and obs.tree.window_title == "W"
    b._exec.shutdown(wait=False)


def test_capture_skips_screencapturekit_after_a_timeout(monkeypatch):
    from voyager.backend import macwin

    calls = []

    def sck(wid, timeout_s):
        calls.append(("sck", timeout_s))
        raise macwin.CaptureError("ScreenCaptureKit timed out")

    def cli(wid, timeout_s):
        calls.append(("cli", timeout_s))
        return b"jpeg"

    monkeypatch.setattr(macwin, "_sck_hung", False)
    monkeypatch.setattr(macwin, "screen_recording_granted", lambda: True)
    monkeypatch.setattr(macwin, "_capture_sck", sck)
    monkeypatch.setattr(macwin, "_capture_cli", cli)
    assert macwin.capture_window(7) == b"jpeg"
    assert macwin.capture_window(7) == b"jpeg"
    assert calls == [("sck", macwin.SCK_TIMEOUT_S), ("cli", 5.0), ("cli", 5.0)]


def test_capture_keeps_screencapturekit_after_other_errors(monkeypatch):
    from voyager.backend import macwin

    calls = []

    def sck(wid, timeout_s):
        calls.append("sck")
        raise macwin.CaptureError(f"window {wid} is not capturable")

    monkeypatch.setattr(macwin, "_sck_hung", False)
    monkeypatch.setattr(macwin, "screen_recording_granted", lambda: True)
    monkeypatch.setattr(macwin, "_capture_sck", sck)
    monkeypatch.setattr(macwin, "_capture_cli", lambda wid, timeout_s: b"jpeg")
    macwin.capture_window(7)
    macwin.capture_window(7)
    assert calls == ["sck", "sck"]
