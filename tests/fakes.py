"""A scripted backend for pipeline tests: observations change according to actions."""
from __future__ import annotations

from voyager.backend.base import Backend, Observation, TargetInfo


class FakeBackend(Backend):
    platform = "mac"

    def __init__(self, pages: dict[str, str], start: str, transitions: dict[tuple[str, str], str], shot: bytes | None = None, rich: set[str] | None = None):
        """pages: name -> AX text; transitions: (page, action-key) -> next page. action-key is
        e.g. 'click:Chat with us' (by element name) or 'set:Type your message'."""
        self.pages = pages
        self.page = start
        self.transitions = transitions
        self.shot = shot
        self.rich = rich or set()  # element names whose setValue is silently ignored (rich editors)
        self.actions: list[str] = []
        self.values: dict[str, str] = {}
        self.children: dict[str, str] = {}  # name -> child text line added by paste/typeText
        self.focused: str | None = None
        self.obs_count = 0

    def _text(self) -> str:
        raw = self.pages[self.page]
        for name, val in self.values.items():
            raw = raw.replace(f"(settable) {name}\n", f"(settable) {name}, Value: {val}\n")
        for name, child in self.children.items():
            lines = raw.split("\n")
            for i, line in enumerate(lines):
                if line.strip().endswith(f"(settable) {name}") or f"(settable) {name}," in line:
                    indent = line[: len(line) - len(line.lstrip("\t"))]
                    lines.insert(i + 1, f"{indent}\t999 text {child}")
                    break
            raw = "\n".join(lines)
        return raw

    def _obs(self, shot=False) -> Observation:
        self.obs_count += 1
        return Observation.from_text(self._text(), screenshot=self.shot if shot else None)

    def _apply(self, key: str) -> Observation:
        self.actions.append(key)
        nxt = self.transitions.get((self.page, key))
        if nxt:
            self.page = nxt
        return self._obs()

    async def list_targets(self):
        return [TargetInfo("app", "Google Chrome", app="Google Chrome", running=True)]

    async def bind(self, target):
        return self._obs()

    async def observe_ax(self, scope="action"):
        return self._obs()

    async def observe_full(self):
        return self._obs(shot=True)

    async def screenshot(self):
        return self.shot or b""

    async def click(self, index=None, xy=None, *, count=1, button="left", mouse=False):
        if index is None:
            return self._apply(f"click_xy:{xy}")
        el = self._obs().tree.get(index)
        if el is not None and el.is_text_input:
            self.focused = el.name
        return self._apply(f"click:{el.name if el else index}")

    async def set_value(self, index, value):
        el = self._obs().tree.get(index)
        from voyager.backend.base import ElementError

        if el is None or not el.settable:
            raise ElementError("Cannot set a value for an element that is not settable")
        if el.name not in self.rich:
            self.values[el.name] = value
        return self._apply(f"set:{el.name}")

    async def paste(self, text, index=None):
        if self.focused:
            self.children[self.focused] = text
        return self._apply(f"paste:{text}")

    async def type_text(self, text, index=None):
        if self.focused:
            self.children[self.focused] = text
        return self._apply(f"type:{text}")

    async def press_key(self, key, index=None):
        return self._apply(f"key:{key}")

    async def scroll(self, direction, amount=1, index=None, xy=None):
        return self._apply(f"scroll:{direction}")
