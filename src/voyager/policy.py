"""Hard rules no model can talk its way past, brief enforcement, budgets, lock and kill switch."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ax import AXTree, Element, host_of
from .brief import Brief
from .config import Settings
from .plan import ENTER_KEYS, Step

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DIGIT_RUN = re.compile(r"(?:\d[ -]?){13,19}")
_CODE_ASK = re.compile(
    r"verification code|one[- ]time (?:code|passcode|password)|security code|enter the code|passcode|\botp\b|\b2fa\b|\bmfa\b|authentication code",
    re.I,
)
_CODE_LIKE = re.compile(r"^\s*\d{4,8}\s*$")
_SCREEN_SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{30,}"
)


class PolicyViolation(Exception):
    pass


class Aborted(Exception):
    pass


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def secret_in_text(text: str, page_text: str = "") -> str | None:
    """Return a reason if ``text`` looks like a secret that must never be typed."""
    for m in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) in (15, 16) and luhn_ok(digits):
            return "text contains a Luhn-valid 15/16 digit number (card number)"
    if _SSN.search(text):
        return "text contains an SSN-shaped number"
    if _CODE_LIKE.match(text) and _CODE_ASK.search(page_text):
        return "text is a 4-8 digit code and the page asks for a code"
    return None


def screen_secret(text: str) -> str | None:
    """Return a label if the screen shows key material that must not be sent to any model."""
    m = _SCREEN_SECRET.search(text)
    if m:
        return "private key or API token visible on screen"
    return None


def host_allowed(host: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True
    if not host:
        return False
    host = host.lower()
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allowed)


@dataclass
class Budget:
    max_steps: int
    max_seconds: float
    max_consecutive_failures: int
    steps: int = 0
    started: float = field(default_factory=time.time)
    consecutive_failures: int = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > self.max_steps:
            raise PolicyViolation(f"budget: more than {self.max_steps} steps in this run")
        if time.time() - self.started > self.max_seconds:
            raise PolicyViolation(f"budget: run exceeded {self.max_seconds / 60:.0f} minutes")

    def failed(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise PolicyViolation(f"budget: {self.consecutive_failures} consecutive failed verifications")

    def succeeded(self) -> None:
        self.consecutive_failures = 0


class KillSwitch:
    """Set from cu_stop or the global hotkey; checked between pipeline stages."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def trip(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._event.set)
        else:
            self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def check(self) -> None:
        if self._event.is_set():
            raise Aborted("stopped by kill switch")


class DriverLock:
    """One driver per desktop. ``<data_dir>/driver.lock`` holds the owning pid."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise PolicyViolation(f"another driver (pid {pid}) holds {self.path}")
            self.path.unlink(missing_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        self.held = True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


def _pid_alive(pid: int) -> bool:
    from .proc import pid_alive

    return pid_alive(pid)


class Policy:
    def __init__(self, settings: Settings, brief: Brief, kill: KillSwitch):
        self.settings = settings
        self.brief = brief
        self.kill = kill
        self.budget = Budget(settings.max_steps, settings.max_minutes * 60, settings.max_consecutive_failures)

    # ---- site & app ------------------------------------------------------------------
    def check_screen(self, tree: AXTree) -> None:
        """Hard rule: never ship a screen that shows key material to an eyes provider."""
        reason = screen_secret(tree.raw)
        if reason:
            raise PolicyViolation(f"{reason}; close or hide it before continuing")

    def check_site(self, tree: AXTree) -> None:
        if not self.brief.allowed_sites:
            return
        host = tree.host
        if host is None:
            if self.brief.site_check == "strict":
                raise PolicyViolation(
                    "brief lists allowed_sites but no page URL is readable from this window; "
                    "bind a browser window or set site_check: lenient in the brief"
                )
            return
        if not host_allowed(host, self.brief.allowed_sites):
            raise PolicyViolation(f"host {host!r} is not in the brief's allowed_sites {self.brief.allowed_sites}")

    def check_app(self, app: str, title: str = "") -> None:
        allowed = self.brief.allowed_apps or self.settings.allowed_apps
        if allowed and not any(a.lower() in (app or "").lower() or a.lower() in (title or "").lower() for a in allowed):
            raise PolicyViolation(f"app {app!r} is not in allowed_apps {allowed}")

    def check_url(self, url: str) -> None:
        if not host_allowed(host_of(url), self.brief.allowed_sites):
            raise PolicyViolation(f"{url!r}: host not in allowed_sites")

    # ---- actions ------------------------------------------------------------------------
    def check_action(self, step: Step, tree: AXTree, element: Element | None) -> None:
        self.kill.check()
        self.check_site(tree)
        if step.do == "key" and step.key and step.key.lower() in ENTER_KEYS:
            raise PolicyViolation("Enter in a text field is a commit; use a commit step")
        if step.do == "type":
            if element is not None and element.password_marker:
                raise PolicyViolation("refusing to type into a password field")
            reason = secret_in_text(step.text or "", tree.visible_text(4000))
            if reason:
                raise PolicyViolation(f"refusing to type: {reason}")

    def password_noul_needed(self, element: Element | None) -> bool:
        return element is not None and not element.password_marker and (element.password_hint or element.role == "unknown")
