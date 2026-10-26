import pytest

from voyager.brief import base_domain, find_brief
from voyager.config import Settings
from voyager.operator import Operator
from voyager.policy import PolicyViolation


def test_base_domain():
    assert base_domain("flights.google.com") == "google.com"
    assert base_domain("www.google.com") == "google.com"
    assert base_domain("shop.example.co.uk") == "example.co.uk"
    assert base_domain("localhost") == "localhost"


def test_inline_brief(tmp_path):
    b = find_brief(None, tmp_path, {"goal": "x", "must_pause_for": ["booking"]})
    assert b.name == "inline" and b.must_pause_for == ["booking"] and b.autonomy == "approve_each_send"
    assert "password" in b.never_share


async def test_open_refuses_off_allowlist_and_unknown_browser(tmp_path, monkeypatch):
    from tests.fakes import FakeBackend
    from voyager.eyes.local import LocalEyes

    s = Settings(data_dir=tmp_path, allowed_apps=["Google Chrome"])
    op = Operator(s, backend=FakeBackend({"p": 'Window: "x", App: Google Chrome.\n0 standard window x\n'}, "p", {}), eyes=LocalEyes())
    with pytest.raises(PolicyViolation, match="allowed_sites"):
        await op.open("https://evil.example", brief_inline={"goal": "g", "allowed_sites": ["google.com"]})
    monkeypatch.setattr("voyager.launch.installed_browsers", lambda: [])
    with pytest.raises(PolicyViolation, match="browser"):
        await op.open("https://google.com", brief_inline={"goal": "g"})
    assert op.brief.allowed_sites == ["google.com"]  # derived from the URL
