"""Plan schema (validated with pydantic) and result types returned to the planner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Do = Literal["click", "double_click", "type", "key", "scroll", "wait_for", "find", "read", "commit"]
NEEDS_EXPECT = {"click", "double_click", "key", "scroll", "wait_for"}
ENTER_KEYS = {"return", "enter", "kp_enter", "kp_return"}


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    do: Do
    target: str | None = Field(default=None, description="The element, described by visible label and location")
    expect: str | None = Field(default=None, description="An observable statement that is true after the step")
    text: str | None = Field(default=None, description="type: the exact text to enter")
    key: str | None = Field(default=None, description="key/commit: a key or chord in xdotool syntax, e.g. 'Tab', 'ctrl+a'")
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int = Field(default=1, ge=1, le=20, description="scroll: pages (macOS) / 500px units")
    summary: str | None = Field(default=None, description="commit: exactly what is sent or submitted")
    timeout_s: float = Field(default=30, ge=1, le=300, description="wait_for: how long to wait")
    region: str | None = Field(default=None, description="read: the region whose text to return, e.g. 'the chat transcript'")
    within: str | None = Field(default=None, alias="in", description="find: the scrollable list or panel to search, e.g. 'the chats list' (default: the page)")
    then: Literal["click", "double_click", "none"] = Field(default="click", description="find: what to do with the item once found")
    max_scrolls: int = Field(default=10, ge=0, le=40, description="find: pages to scroll before giving up")
    when: str | None = Field(default=None, alias="if", description="Run this step only if this observable statement is true now, e.g. 'a cookie banner is shown'; otherwise skip it")

    @model_validator(mode="after")
    def _check(self) -> "Step":
        if self.do in NEEDS_EXPECT and not self.expect:
            raise ValueError(f"step '{self.do}' needs an 'expect'")
        if self.do == "find" and self.then != "none" and not self.expect:
            raise ValueError("step 'find' that clicks needs an 'expect' (what is true after the click)")
        if self.do in {"click", "double_click", "type", "find"} and not self.target:
            raise ValueError(f"step '{self.do}' needs a 'target'")
        if self.do == "type" and self.text is None:
            raise ValueError("step 'type' needs 'text'")
        if self.do == "key" and not self.key:
            raise ValueError("step 'key' needs 'key'")
        if self.do == "key" and self.key.lower() in ENTER_KEYS:
            raise ValueError("Enter is a commit: use {'do': 'commit', 'key': 'Return', 'summary': ...}")
        if self.do == "scroll" and not self.direction:
            raise ValueError("step 'scroll' needs 'direction'")
        if self.do == "commit" and not self.summary:
            raise ValueError("step 'commit' needs a 'summary' of what is sent or submitted")
        if self.do == "commit" and not (self.target or self.key):
            raise ValueError("step 'commit' needs a 'target' (button) or 'key' (Return)")
        if self.do == "commit" and self.when:
            raise ValueError("a commit cannot be conditional")
        if self.do == "read" and not self.region:
            raise ValueError("step 'read' needs a 'region'")
        return self


class Plan(BaseModel):
    brief: str | None = Field(default=None, description="Name of the brief in briefs/ (without .yaml)")
    steps: list[Step] = Field(min_length=1)
    stop_if: list[str] = Field(default_factory=list, description="Observable conditions that halt the plan")

    @model_validator(mode="after")
    def _commit_last(self) -> "Plan":
        for i, s in enumerate(self.steps):
            if s.do == "commit" and i != len(self.steps) - 1:
                raise ValueError("a commit step must be the last step of a plan")
        return self


Status = Literal["done", "ready_to_commit", "stopped", "failed", "needs_help"]


@dataclass
class Grounded:
    """A grounded target: an element (by stable key) or a screenshot coordinate."""

    kind: Literal["element", "point"]
    index: int | None = None
    key: str | None = None
    label: str = ""
    confidence: float = 0.0
    rung: int = 0
    xy: tuple[int, int] | None = None
    shuffled_confidence: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "index": self.index, "key": self.key, "label": self.label,
            "confidence": round(self.confidence, 3), "rung": self.rung, "xy": self.xy,
        }


@dataclass
class PendingCommit:
    plan: Plan
    step_index: int
    step: Step
    target: Grounded | None
    typed_text: str | None = None
    brief_name: str | None = None
    created_at: float = 0.0


@dataclass
class PlanResult:
    status: Status
    step_index: int
    reason: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    diff_text: str = ""
    page_text: str = ""
    title: str = ""
    url: str | None = None
    screenshot_path: str | None = None
    candidates: list[str] = field(default_factory=list)
    pending_commit: dict | None = None
    steps_completed: int = 0
    reads: list[dict] = field(default_factory=list)  # results of read steps, in order
    skipped: list[int] = field(default_factory=list)  # conditional steps whose condition was false
    receipts: list[dict] = field(default_factory=list)  # per step: what was picked, the check, what changed

    def to_dict(self) -> dict:
        d = {
            "status": self.status, "step_index": self.step_index, "steps_completed": self.steps_completed,
            "reason": self.reason, "title": self.title, "url": self.url, "timings": self.timings,
        }
        if self.diff_text:
            d["changes"] = self.diff_text
        if self.page_text:
            d["page_text"] = self.page_text
        if self.screenshot_path:
            d["screenshot_path"] = self.screenshot_path
        if self.candidates:
            d["candidates"] = self.candidates
        if self.pending_commit:
            d["pending_commit"] = self.pending_commit
        if self.reads:
            d["reads"] = self.reads
        if self.skipped:
            d["skipped_steps"] = self.skipped
        if self.receipts:
            d["receipts"] = self.receipts
        if self.answers:
            d["eyes"] = self.answers
        return d
