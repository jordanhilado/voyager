import json

import httpx
import pytest
from PIL import Image

from voyager.config import Settings
from voyager.eyes import make_eyes
from voyager.eyes.base import FakeEyes, box, choice, noul
from voyager.eyes.claude import _normalize
from voyager.eyes.fallback import JevWithFallback
from voyager.eyes.jev import JevEyes
from voyager.eyes.local import LocalEyes
from voyager.images import fit, to_jpeg


def _png(w, h):
    img = Image.new("RGB", (w, h), "white")
    import io

    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()


async def test_jev_text_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization"); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 10, "output_tokens": 1}, "answers": {"q": {"type": "noul", "noul": 0.9}}})

    s = Settings(typesafe_api_key="k")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer k"})
    eyes = JevEyes(s, http=http)
    a = await eyes.ask({"x": 1}, {"q": noul("is it?")})
    assert a["q"]["noul"] == 0.9
    assert seen["url"].endswith("/v1/systemone") and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "jev-1.14.0" and seen["body"]["questions"]["q"]["type"] == "noul"
    assert eyes.last_latency_ms >= 0 and eyes.calls == 1


async def test_jev_image_request_is_multipart_with_header():
    seen = {}

    def handler(req: httpx.Request):
        seen["ct"] = req.headers.get("content-type", ""); seen["danger"] = req.headers.get("typesafe-danger-zone-images")
        seen["raw"] = req.content
        return httpx.Response(200, json={"answers": {"b": {"type": "box", "box": {"top": 0.1, "bottom": 0.2, "left": 0.3, "right": 0.4}, "confidence": None}}})

    eyes = JevEyes(Settings(typesafe_api_key="k"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    a = await eyes.ask({"note": "x"}, {"b": box("the thing in `shot`")}, {"shot": _png(100, 100)})
    assert seen["ct"].startswith("multipart/form-data") and seen["danger"] == "true"
    assert b'name="request"' in seen["raw"] and b'name="part-shot"' in seen["raw"]
    body = seen["raw"].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0]
    req = json.loads(body)
    ref = req["state"]["shot"]
    assert req["assets"][ref] == {"type": "image", "part": "part-shot"}
    assert a["b"]["box"]["top"] == 0.1 and a["b"]["confidence"] is None


async def test_jev_retries_on_429():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.1}}})

    eyes = JevEyes(Settings(typesafe_api_key="k"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    a = await eyes.ask("s", {"q": noul("?")})
    assert calls["n"] == 2 and a["q"]["noul"] == 0.1


def test_fit_rules():
    small = fit(Image.new("RGB", (50, 40)))
    assert small.size[0] >= 280 and small.size[1] >= 280
    big = fit(Image.new("RGB", (4112, 2466)))
    assert max(big.size) <= 1568 and big.width * big.height <= 2_458_624
    assert to_jpeg(big)[:2] == b"\xff\xd8"


def test_claude_normalize():
    q = choice("which?", {"e0": "a", "e1": "b", "none": "n"})
    a = _normalize(q, {"choice": "e1", "confidence": 0.9})
    assert a["choice"] == "e1" and abs(sum(a["probabilities"].values()) - 1) < 1e-6
    assert _normalize(q, {"choice": "bogus", "confidence": 0.9})["choice"] == "none"
    assert _normalize(noul("x"), {"probability": 1.7})["noul"] == 1.0


async def test_local_eyes_picks_by_label():
    eyes = LocalEyes()
    crit = {"e0": 'button "Google Search"', "e1": 'button "Chat with us" · in container "Help"', "e2": 'link "About"', "none": "none"}
    a = await eyes.ask({"visible_text": "Help Chat with us"}, {"pick": choice("Which listed element is the 'Chat with us' button?", crit), "v": noul("Is this true: a 'Chat with us' button is visible")})
    assert a["pick"]["choice"] == "e1" and a["pick"]["confidence"] >= 0.8
    assert a["v"]["noul"] >= 0.75


def test_jev_key_is_required(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        make_eyes(Settings(anthropic_api_key="a"))


def test_make_eyes_is_jev_with_claude_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eyes = make_eyes(Settings(typesafe_api_key="k", anthropic_api_key="a"))
    assert isinstance(eyes, JevWithFallback) and eyes.jev.name == "jev" and eyes.fallback.name == "claude"
    assert make_eyes(Settings(typesafe_api_key="k")).fallback is None


def _jev(handler):
    return JevEyes(Settings(typesafe_api_key="k"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_claude_answers_only_when_jev_fails():
    jev_up = {"ok": False}

    def handler(req):
        if not jev_up["ok"]:
            return httpx.Response(401, text="bad key")
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.2}}})

    claude = FakeEyes(lambda s, q, i: {"q": {"type": "noul", "noul": 0.9}})
    claude.name = "claude"
    eyes = JevWithFallback(_jev(handler), claude)
    seen = []
    eyes.on_call = seen.append
    assert (await eyes.ask("s", {"q": noul("?")}))["q"]["noul"] == 0.9
    jev_up["ok"] = True
    assert (await eyes.ask("s", {"q": noul("?")}))["q"]["noul"] == 0.2  # back on Jev for the next call
    assert [r["provider"] for r in seen] == ["claude", "jev"] and eyes.fallbacks == 1 and len(claude.log) == 1


async def test_jev_failure_raises_without_fallback():
    eyes = JevWithFallback(_jev(lambda req: httpx.Response(500, text="down")))
    with pytest.raises(RuntimeError, match="TypeSafe 500"):
        await eyes.ask("s", {"q": noul("?")})
