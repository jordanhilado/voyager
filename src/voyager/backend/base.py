"""Backend interface: everything the pipeline needs from the desktop layer.

``MacNativeBackend`` implements it over macOS accessibility. A Windows UI Automation backend
can implement the same interface without touching the pipeline.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from ..ax import AXTree, parse_ax


@dataclass
class Observation:
    tree: AXTree
    raw: str
    screenshot: bytes | None = None  # JPEG/PNG bytes, window-relative
    shot_size: tuple[int, int] | None = None  # (w, h) pixels
    t_ms: float = 0.0  # observation cost
    partial: bool = False  # True when the tree covers only the area around the viewport
    at: float = field(default_factory=time.time)

    @classmethod
    def from_text(cls, text: str, screenshot: bytes | None = None, t_ms: float = 0.0) -> "Observation":
        obs = cls(tree=parse_ax(text), raw=text, screenshot=screenshot, t_ms=t_ms)
        if screenshot:
            from ..images import load

            obs.shot_size = load(screenshot).size
        return obs


@dataclass
class TargetInfo:
    kind: str  # "app" | "window"
    id: str | int
    title: str = ""
    app: str = ""
    url: str | None = None
    running: bool | None = None

    def describe(self) -> str:
        bits = [self.kind, str(self.id)]
        if self.app:
            bits.append(f"app {self.app!r}")
        if self.title:
            bits.append(f"title {self.title!r}")
        if self.url:
            bits.append(f"url {self.url}")
        return " · ".join(bits)


class RuntimeError_(Exception):
    """A runtime-level failure in the desktop layer (an app, window or action failed)."""


class ElementError(RuntimeError_):
    """The element index was stale or the element is not actionable; re-ground."""


class Backend(abc.ABC):
    platform: str = "unknown"  # "mac" | "windows" | "linux"
    # Settling: the page is settled once its hash has not changed for ``settle_quiet_s`` (0 means
    # two equal reads in a row), polling every ``settle_poll_s``.
    settle_quiet_s: float = 0.0
    settle_poll_s: float = 0.12

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def list_targets(self) -> list[TargetInfo]: ...

    @abc.abstractmethod
    async def bind(self, target: TargetInfo) -> Observation: ...

    @abc.abstractmethod
    async def observe_ax(self, scope: str = "action") -> Observation:
        """``scope``: "action" (may be limited to the area around the viewport) or "full"."""

    @abc.abstractmethod
    async def observe_full(self) -> Observation: ...

    @abc.abstractmethod
    async def screenshot(self) -> bytes: ...

    @abc.abstractmethod
    async def click(self, index: int | None = None, xy: tuple[int, int] | None = None, *, count: int = 1, button: str = "left", mouse: bool = False) -> Observation:
        """``mouse``: prefer a real pointer click over an accessibility press (used on retries)."""

    @abc.abstractmethod
    async def set_value(self, index: int, value: str) -> Observation: ...

    @abc.abstractmethod
    async def paste(self, text: str, index: int | None = None) -> Observation: ...

    @abc.abstractmethod
    async def type_text(self, text: str, index: int | None = None) -> Observation: ...

    @abc.abstractmethod
    async def press_key(self, key: str, index: int | None = None) -> Observation: ...

    @abc.abstractmethod
    async def scroll(self, direction: str, amount: int = 1, index: int | None = None, xy: tuple[int, int] | None = None) -> Observation: ...

    async def reset(self) -> None: ...
