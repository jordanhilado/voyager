"""Every Eyes question template and every threshold lives here.

Thresholds can be overridden by ``<data_dir>/thresholds.yaml`` (written by
``voyager calibrate``). Wording is deliberately literal: Jev reads literally.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import yaml


@dataclass
class Thresholds:
    T_PICK: float = 0.80      # Choice confidence to act on a pick
    T_PRESENT: float = 0.80   # Presence Noul to trust a Box
    T_BOX: float = 0.70       # Box confidence to click without the crop rung
    T_VERIFY: float = 0.75    # Verification Noul to call a step done
    T_STOP: float = 0.50      # Stop condition Noul to halt the plan
    T_COMMIT: float = 0.90    # Both shuffled answers must clear this before a commit
    T_PASSWORD: float = 0.20  # Password-field Noul above this refuses typing
    T_PAUSE: float = 0.70     # Consent/offer/must_pause_for Noul above this pauses the plan
    T_LEAK: float = 0.50      # "Draft contains never_share info" Noul above this refuses typing
    T_WITHIN_BRIEF: float = 0.90  # auto_send_within_brief needs this
    T_IF: float = 0.50        # A conditional step runs when its condition Noul reaches this

    @classmethod
    def load(cls, path: Path | None) -> "Thresholds":
        t = cls()
        if path and path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            for k, v in data.items():
                if hasattr(t, k):
                    setattr(t, k, float(v))
        return t

    def dump(self) -> dict:
        return asdict(self)


# ---- Question templates -------------------------------------------------------

PICK = (
    'Which listed element is {target}? Options describe one UI element each, in top-to-bottom '
    'order of the page. Answer "none" if no listed element is it.'
)
PRESENT = "Is {target} present on the page described in the state?"
VERIFY = "Is this statement true of the page described in the state right now: {expect}"
STOP = "Is this true of the page described in the state: {cond}"
BLOCKER_CRITERIA = {
    "none": "the page is usable: no dialog, error, sign-in wall, CAPTCHA or loading indicator blocks it",
    "sign_in_wall": "the page asks the user to sign in, log in or enter a password before continuing",
    "error_message": "an error message says something went wrong or a request failed",
    "modal_dialog": "a modal dialog, pop-up, filter panel or overlay is open on top of the page",
    "captcha": "a CAPTCHA or human-verification challenge is shown",
    "loading": "the page or panel is still loading or shows a spinner or skeleton",
}
BLOCKER = "Which best describes the current state of the page?"
PAUSE_CONSENT = (
    "Is the page or chat asking the user, right now, to consent to a credit check, a hard inquiry, a legal "
    "agreement or new terms, or to confirm an irreversible action (a prompt with agree/accept/confirm controls)? "
    "Cookie banners and ordinary menus do not count."
)
PAUSE_OFFER = (
    "Is the page or chat asking the user to accept or decline a specific offer, counteroffer, quote, contract or "
    "terms right now (a decision prompt with accept/decline or agree/continue controls), as opposed to merely "
    "displaying prices, products, options or menus?"
)
PAUSE_ITEM = "Is this true of the page described in the state right now: {item}"
PASSWORD_FIELD = (
    "Is the element described in `field` a password, PIN, passcode, or one-time verification code field?"
)
LEAK = "Does the text in `draft` contain any of the following kinds of information: {never_share}?"
WITHIN_BRIEF = (
    "Does the text in `draft` contain only information listed in `may_share` and requests listed in `ask`, "
    "and nothing else that is personal, financial or sensitive?"
)
CONDITION = "Is this true of the page described in the state right now: {cond}"
WAIT_UNTIL = "Is this true of the page described in the state: {statement}"
REGION_PICK = 'Which listed container is {region}? Answer "none" if none of them is.'
BOX_WINDOW = "The location of {target} in `screenshot`."
BOX_CROP = "The location of {target} in `crop`."
BIND_PICK = "Which listed window or application is {target}?"
