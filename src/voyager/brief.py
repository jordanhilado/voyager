"""Task briefs: one YAML per task. The brief is the contract for what may be done and shared."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Brief(BaseModel):
    name: str = "default"
    goal: str = ""
    allowed_sites: list[str] = Field(default_factory=list, description="Host suffixes, e.g. americanexpress.com")
    allowed_apps: list[str] = Field(default_factory=list, description="App or window names the run may bind")
    ask: str | list[str] = ""
    may_share: list[str] = Field(default_factory=list)
    must_pause_for: list[str] = Field(default_factory=list)
    never_share: list[str] = Field(default_factory=lambda: ["password", "SSN", "full card number", "one-time codes", "security answers"])
    autonomy: Literal["approve_each_send", "auto_send_within_brief"] = "approve_each_send"
    tone: str = ""
    site_check: Literal["strict", "lenient"] = Field(
        default="strict",
        description="strict: refuse actions when no URL is readable and allowed_sites is set; lenient: allow",
    )

    @classmethod
    def load(cls, path: Path) -> "Brief":
        data = yaml.safe_load(path.read_text()) or {}
        data.setdefault("name", path.stem)
        return cls(**data)

    @classmethod
    def permissive(cls) -> "Brief":
        return cls(name="default", goal="(no brief)", site_check="lenient")

    def summary(self) -> dict:
        return self.model_dump()


def base_domain(host: str) -> str:
    """flights.google.com -> google.com (good enough for allowlists; co.uk-style hosts keep 3 labels)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def find_brief(name: str | None, briefs_dir: Path, inline: dict | None = None) -> Brief:
    if inline:
        data = dict(inline)
        data.setdefault("name", name or "inline")
        return Brief(**data)
    if not name:
        return Brief.permissive()
    p = Path(name)
    if not p.exists():
        p = briefs_dir / f"{name}.yaml"
    if not p.exists():
        p = briefs_dir / f"{name}.yml"
    if not p.exists():
        raise FileNotFoundError(f"brief {name!r} not found in {briefs_dir}")
    return Brief.load(p)
