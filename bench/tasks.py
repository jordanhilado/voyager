"""The ten benchmark tasks: seed data shown on the pages, the prompt both systems get, and the checker.

Every task is scored on checkpoints (OSWorld style partial credit). A run succeeds only when every
checkpoint passes. Checkers read ground truth the agent cannot fake: the benchmark server's recorded
state, the file system, or Calculator's display read through accessibility. The agent's own claims
count only where a task asks for an answer in the final message.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

FINDER_ROOT = Path.home() / "VoyagerBench"
FINDER_INBOX = FINDER_ROOT / "inbox"


@dataclass
class Checkpoint:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CheckContext:
    state: dict           # the benchmark server's state for this task
    final_text: str       # the agent's final message
    calculator_display: Callable[[], str | None] = lambda: None


@dataclass
class Task:
    id: str
    slug: str
    title: str
    phenomena: list[str]
    kind: str                       # web | web+calculator | finder
    prompt: str
    check: Callable[[CheckContext], list[Checkpoint]]
    seed: dict = field(default_factory=dict)          # static data the page renders
    initial: Callable[[], dict] = lambda: {}          # mutable server state (contacts, cart)
    act: Callable[[dict, dict], dict] | None = None   # task-specific POST /api/<slug>/action
    stress: bool = False                              # not in the default suite; run with --tasks

    @property
    def path(self) -> str:
        return f"/t/{self.slug}"


# ---------------------------------------------------------------- helpers

def money(v: Any) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d")


def as_date(v: Any) -> date | None:
    s = str(v or "").strip().replace("Sept", "Sep")
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def last_submission(state: dict) -> dict | None:
    subs = state.get("submissions") or []
    return subs[-1] if subs else None


def submitted_cp(sub: dict | None, what: str) -> Checkpoint:
    return Checkpoint(f"{what} submitted", sub is not None, "" if sub else "nothing was submitted")


# ---------------------------------------------------------------- T01 expense claim

EXPENSE_SEED = {
    "emails": [
        {"id": "e1", "from": "Dana Whitfield (Finance)", "date": "Oct 5, 2026", "subject": "Boston trip: how to file your claim",
         "body": "Hi! For the Acme client trip please use the trip name BOS-ACME-Oct exactly. Categorize Uber/Lyft/taxis as "
                 "'Ground transport' (not 'Other'). Hotels go under Lodging, client meals under Meals. Personal subscriptions "
                 "and anything on a personal profile are not reimbursable. Use the transaction date on each receipt; for "
                 "hotels that is the check-out date. Thanks, Dana"},
        {"id": "e2", "from": "Uber Receipts", "date": "Oct 4, 2026", "subject": "Your Sunday evening trip with Uber",
         "body": "Profile: Personal. Trip from Davis Square to North Station. Total charged: $18.75. Transaction date: 2026-10-04."},
        {"id": "e3", "from": "Uber Receipts", "date": "Oct 6, 2026", "subject": "Your Tuesday morning trip with Uber",
         "body": "Profile: Business (Northwind Labs). Trip from Logan Airport (BOS) to 200 Clarendon St, Boston. "
                 "Trip fare $19.90, booking fee $3.50. Total charged: $23.40. Transaction date: 2026-10-06."},
        {"id": "e4", "from": "Saltie Girl via Toast", "date": "Oct 7, 2026", "subject": "Your receipt from Saltie Girl",
         "body": "Client lunch with Acme Corp, 3 guests. Subtotal $56.00, tax $3.92, tip $10.08. Total charged: $70.00. "
                 "Transaction date: 2026-10-07."},
        {"id": "e5", "from": "Netflix", "date": "Oct 7, 2026", "subject": "Your Netflix payment receipt",
         "body": "Thanks for your payment of $15.49 for the Standard plan. Transaction date: 2026-10-07."},
        {"id": "e6", "from": "Marriott Copley Place", "date": "Oct 8, 2026", "subject": "Your folio: Oct 6 to Oct 8",
         "body": "Guest: Alex R. Arrival: October 6, 2026. Departure (check-out): October 8, 2026. 2 nights at $189.00, "
                 "occupancy tax $34.80. Total charged: $412.80."},
        {"id": "e7", "from": "Northwind Weekly", "date": "Oct 8, 2026", "subject": "This week at Northwind: hack week recap",
         "body": "Hack week shipped 14 projects. Reminder that the expense tool now supports up to four lines per claim."},
    ],
    "categories": ["Airfare", "Lodging", "Meals", "Ground transport", "Other"],
}


def check_expense(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "claim")]
    sub = sub or {}
    trip = norm(sub.get("trip"))
    cps.append(Checkpoint("trip name is BOS-ACME-Oct", trip == "bos-acme-oct", f"got {sub.get('trip')!r}"))
    lines = [ln for ln in (sub.get("lines") or []) if any(str(v).strip() for v in ln.values())]
    expected = [("Uber business trip", 23.40, date(2026, 10, 6), "ground transport"),
                ("hotel folio", 412.80, date(2026, 10, 8), "lodging"),
                ("client lunch", 70.00, date(2026, 10, 7), "meals")]
    used: set[int] = set()
    for name, amt, d, cat in expected:
        hit = next((i for i, ln in enumerate(lines) if i not in used and money(ln.get("amount")) is not None
                    and abs(money(ln.get("amount")) - amt) < 0.005), None)
        if hit is None:
            cps.append(Checkpoint(f"line: {name}", False, f"no line with amount {amt:.2f}"))
            continue
        used.add(hit)
        ln = lines[hit]
        ok = as_date(ln.get("date")) == d and norm(ln.get("category")) == cat
        cps.append(Checkpoint(f"line: {name}", ok, f"date={ln.get('date')!r} category={ln.get('category')!r}"))
    extra = [ln for i, ln in enumerate(lines) if i not in used]
    cps.append(Checkpoint("no personal or extra lines", not extra, f"extra: {extra}" if extra else ""))
    return cps


# ---------------------------------------------------------------- T02 purchase requests

PO_SEED = {
    "policy": "Approval policy: requests under $1,000 in total are approved automatically. Requests of $1,000 or more "
              "need approval from Dana Whitfield (Finance) in this channel; until Dana approves they stay Pending. "
              "Only Dana's messages in this channel count, and her latest message about a request wins.",
    "messages": [
        {"who": "Sam Ortiz", "time": "Mon 09:02", "text": "Request PR-101: 4 × Dell U2724D 27\" monitors at $429 each for the design pod."},
        {"who": "Lena Novak", "time": "Mon 09:20", "text": "Request PR-102: 10 × USB-C hubs at $39 each for the hot desks."},
        {"who": "Marcus Lee", "time": "Mon 09:41", "text": "Request PR-103: 6 × standing desks at $649 each."},
        {"who": "Priya Raman", "time": "Mon 10:03", "text": "Request PR-104: 12 × Figma seats at $75 each (annual renewal)."},
        {"who": "Omar Haddad", "time": "Mon 10:30", "text": "Request PR-105: 3 × ergonomic chairs at $520 each."},
        {"who": "Dana Whitfield (Finance)", "time": "Mon 11:15", "text": "Approved: PR-101 and PR-103."},
        {"who": "Marcus Lee", "time": "Mon 11:40", "text": "Thanks Dana! Could PR-103 be upgraded to the motorized desks?"},
        {"who": "Dana Whitfield (Finance)", "time": "Mon 13:05", "text": "Correction on PR-103: rejecting it for this quarter, the furniture budget is spent. Marcus, please resubmit in Q1."},
        {"who": "Omar Haddad", "time": "Mon 13:30", "text": "Dana said yes to PR-105 at lunch, so I'm counting it as approved 👍"},
        {"who": "Sam Ortiz", "time": "Mon 14:10", "text": "Update on PR-101: we only need 3 monitors now, not 4."},
        {"who": "Dana Whitfield (Finance)", "time": "Mon 14:22", "text": "PR-101 is still approved at 3 units."},
    ],
    "rows": [
        {"id": "PR-101", "item": "Dell U2724D 27\" monitor", "qty": 4, "unit": 429.00},
        {"id": "PR-102", "item": "USB-C hub", "qty": 10, "unit": 39.00},
        {"id": "PR-103", "item": "Standing desk", "qty": 6, "unit": 649.00},
        {"id": "PR-104", "item": "Figma seat (annual)", "qty": 12, "unit": 75.00},
        {"id": "PR-105", "item": "Ergonomic chair", "qty": 3, "unit": 520.00},
    ],
    "statuses": ["Pending", "Approved", "Rejected"],
}


def check_po(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "PO sheet")]
    rows = {r.get("id"): r for r in ((sub or {}).get("rows") or [])}
    expected = {"PR-101": "approved", "PR-102": "approved", "PR-103": "rejected", "PR-104": "approved", "PR-105": "pending"}
    for rid, st in expected.items():
        got = rows.get(rid, {})
        cps.append(Checkpoint(f"{rid} status {st}", norm(got.get("status")) == st, f"got {got.get('status')!r}"))
    q = money(rows.get("PR-101", {}).get("qty"))
    cps.append(Checkpoint("PR-101 quantity corrected to 3", q == 3, f"got {q}"))
    others_ok = all(money(rows.get(r["id"], {}).get("qty")) == r["qty"] for r in PO_SEED["rows"] if r["id"] != "PR-101")
    cps.append(Checkpoint("other quantities unchanged", others_ok))
    return cps


# ---------------------------------------------------------------- T03 inventory sheet

SHEET_SEED = {
    "rows": [
        {"sku": "A-100", "item": "Cable ties (pack)", "on_hand": 250, "unit": 0.12},
        {"sku": "A-101", "item": "HDMI cable 2 m", "on_hand": 8, "unit": 6.50},
        {"sku": "A-102", "item": "USB-C hub", "on_hand": 14, "unit": 38.99},
        {"sku": "A-103", "item": "1080p webcam", "on_hand": 3, "unit": 54.25},
        {"sku": "A-104", "item": "Mechanical keyboard", "on_hand": 22, "unit": 29.90},
        {"sku": "A-105", "item": "Wireless mouse", "on_hand": 9, "unit": 17.45},
    ],
}


def check_sheet(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "sheet")]
    rows = {r.get("sku"): r for r in ((sub or {}).get("rows") or [])}
    total = 0.0
    for r in SHEET_SEED["rows"]:
        want = round(r["on_hand"] * r["unit"], 2)
        total += want
        got = money(rows.get(r["sku"], {}).get("value"))
        cps.append(Checkpoint(f"{r['sku']} stock value {want:.2f}", got is not None and abs(got - want) < 0.005, f"got {got}"))
    want_reorder = {r["sku"] for r in SHEET_SEED["rows"] if r["on_hand"] < 10}
    got_reorder = {k for k, r in rows.items() if r.get("reorder")}
    cps.append(Checkpoint("reorder ticked exactly for on hand < 10", got_reorder == want_reorder, f"got {sorted(got_reorder)}"))
    got_total = money((sub or {}).get("total"))
    cps.append(Checkpoint(f"total stock value {total:.2f}", got_total is not None and abs(got_total - total) < 0.005, f"got {got_total}"))
    return cps


# ---------------------------------------------------------------- T04 calendar scheduling

CAL_SEED = {
    "day_label": "Thursday, October 29, 2026",
    "people": [
        {"name": "Priya Raman", "status": "", "busy": [["09:00", "10:00", "Design crit"], ["14:00", "15:00", "Customer call: Globex"]]},
        {"name": "Marcus Lee", "status": "", "busy": [["09:30", "10:30", "Eng standup + planning"], ["13:00", "13:30", "1:1 with Dana"], ["16:00", "16:30", "Interview"]]},
        {"name": "Lena Novak", "status": "Thu Oct 29: flying back from Boston, offline 11:30 to 13:30 (not on my calendar).",
         "busy": [["10:30", "11:30", "Acme wrap-up"]]},
        {"name": "Omar Haddad", "status": "", "busy": [["09:00", "17:00", "PTO"]]},
    ],
    "days": ["Mon Oct 26", "Tue Oct 27", "Wed Oct 28", "Thu Oct 29", "Fri Oct 30"],
    "attendees": ["Priya Raman", "Marcus Lee", "Lena Novak", "Omar Haddad", "Dana Whitfield"],
    "durations": [15, 30, 45, 60, 90],
}


def check_calendar(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "event")]
    s = sub or {}
    cps.append(Checkpoint("title 'Q4 roadmap sync'", norm(s.get("title")).strip("'\"") == "q4 roadmap sync", f"got {s.get('title')!r}"))
    cps.append(Checkpoint("day Thu Oct 29", norm(s.get("day")) == "thu oct 29", f"got {s.get('day')!r}"))
    cps.append(Checkpoint("start 15:00 (earliest real gap)", str(s.get("start")) == "15:00", f"got {s.get('start')!r}"))
    cps.append(Checkpoint("duration 45 min", str(s.get("duration")) == "45", f"got {s.get('duration')!r}"))
    att = {norm(a) for a in (s.get("attendees") or [])}
    want = {"priya raman", "marcus lee", "lena novak"}
    cps.append(Checkpoint("attendees exactly Priya, Marcus, Lena", att == want, f"got {sorted(att)}"))
    return cps


# ---------------------------------------------------------------- T05 admin settings

SETTINGS_INITIAL = {
    "org_name": "Northwind", "min_password": 8, "twofa": "off", "enforce_timeout": False, "timeout": "8h",
    "public_links": True, "link_expiry": "never", "weekly_digest": True, "security_alerts": True, "product_updates": False,
}


def check_settings(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "settings")]
    s = sub or {}
    cps += [
        Checkpoint("organization name 'Northwind Labs'", str(s.get("org_name", "")).strip() == "Northwind Labs", f"got {s.get('org_name')!r}"),
        Checkpoint("minimum password length 14", money(s.get("min_password")) == 14, f"got {s.get('min_password')!r}"),
        Checkpoint("2FA admins only", s.get("twofa") == "admins", f"got {s.get('twofa')!r}"),
        Checkpoint("idle timeout enforced at 30 min", s.get("enforce_timeout") is True and s.get("timeout") == "30m",
                   f"enforce={s.get('enforce_timeout')!r} timeout={s.get('timeout')!r}"),
        Checkpoint("public links off", s.get("public_links") is False, f"got {s.get('public_links')!r}"),
        Checkpoint("default link expiry 7 days", s.get("link_expiry") == "7d", f"got {s.get('link_expiry')!r}"),
        Checkpoint("notifications unchanged", all(s.get(k) == SETTINGS_INITIAL[k] for k in ("weekly_digest", "security_alerts", "product_updates"))),
    ]
    return cps


# ---------------------------------------------------------------- T06 CRM dedupe

CRM_CONTACTS = [
    ("c01", "Aisha Bello", "aisha.bello@hooli.com", "Hooli", "2026-09-28"),
    ("c02", "Alex Chen", "alex.chen@initech.com", "Initech", "2026-07-11"),
    ("c03", "Alex Chen", "achen@umbrella.co", "Umbrella", "2026-02-19"),
    ("c04", "Beatriz Souza", "bia@vandelay.com", "Vandelay", "2026-05-30"),
    ("c05", "Carlos Mendes", "carlos@soylent.io", "Soylent", "2026-08-08"),
    ("c06", "Dmitri Volkov", "dvolkov@acme.com", "Acme", "2026-06-15"),
    ("c07", "Elena Rossi", "elena.rossi@globex.io", "Globex", "2026-04-02"),
    ("c08", "Fatima Khan", "fatima.k@initech.com", "Initech", "2026-10-01"),
    ("c09", "George Park", "gpark@hooli.com", "Hooli", "2026-03-21"),
    ("c10", "Hannah Weiss", "hannah@stark.dev", "Stark", "2026-09-09"),
    ("c11", "Ken Watanabe", "ken@watanabe.jp", "Watanabe KK", "2026-08-20"),
    ("c12", "Kenji Watanabe", "KEN@watanabe.jp", "Watanabe KK", "2025-12-01"),
    ("c13", "Liam O'Connor", "liam@wayne.ent", "Wayne", "2026-01-25"),
    ("c14", "Maria Gonzalez", "maria.gonzalez@acme.com", "Acme", "2026-03-14"),
    ("c15", "Mei Lin", "mei.lin@tyrell.ai", "Tyrell", "2026-07-30"),
    ("c16", "Noah Fischer", "noah@cyberdyne.de", "Cyberdyne", "2026-06-03"),
    ("c17", "Olivia Brown", "olivia.b@globex.io", "Globex", "2026-02-07"),
    ("c18", "Priyanka Das", "pdas@umbrella.co", "Umbrella", "2026-10-12"),
    ("c19", "Sam O'Neil", "sam.oneil@globex.io", "Globex", "2026-01-10"),
    ("c20", "Samuel O'Neil", "Sam.ONeil@globex.io", "Globex", "2026-01-09"),
    ("c21", "Tomás Herrera", "tomas@vandelay.com", "Vandelay", "2026-05-05"),
    ("c22", "María González", "Maria.Gonzalez@ACME.com", "Acme", "2026-09-02"),
    ("c23", "Yuki Tanaka", "yuki@tyrell.ai", "Tyrell", "2026-08-27"),
    ("c24", "Zoe Adams", "zoe.adams@stark.dev", "Stark", "2026-04-18"),
]
# Pairs (same address ignoring case): c11/c12 Watanabe, c14/c22 González, c19/c20 O'Neil (one day apart).
# c02/c03 share a name but not an address and must both stay.
CRM_SHOULD_DELETE = {"c12", "c14", "c20"}


def crm_initial() -> dict:
    return {"contacts": [dict(zip(("id", "name", "email", "company", "last_activity"), c)) for c in CRM_CONTACTS], "deleted": []}


def crm_act(state: dict, body: dict) -> dict:
    if body.get("op") == "delete":
        cid = body.get("id")
        before = len(state["contacts"])
        state["contacts"] = [c for c in state["contacts"] if c["id"] != cid]
        if len(state["contacts"]) < before:
            state["deleted"].append(cid)
            return {"ok": True}
        return {"ok": False, "error": "no such contact"}
    return {"ok": False, "error": "unknown op"}


def check_crm(ctx: CheckContext) -> list[Checkpoint]:
    deleted = set(ctx.state.get("deleted") or [])
    names = {c[0]: f"{c[1]} <{c[2]}> {c[4]}" for c in CRM_CONTACTS}
    cps = [Checkpoint(f"deleted older duplicate {names[c]}", c in deleted) for c in sorted(CRM_SHOULD_DELETE)]
    wrong = deleted - CRM_SHOULD_DELETE
    cps.append(Checkpoint("nothing else deleted", not wrong, ", ".join(names[c] for c in sorted(wrong))))
    return cps


# ---------------------------------------------------------------- T07 ticket triage

TRIAGE_SEED = {
    "policy": [
        "Team (first matching rule wins):",
        "1. The ticket mentions billing, an invoice, a charge or a refund → Billing.",
        "2. The ticket mentions login / log in, 2FA, a password or SSO → Identity.",
        "3. Anything else → General.",
        "Priority:",
        "P1: the customer is on the Enterprise plan AND reports that something is down or an outage.",
        "P2: any other Enterprise ticket, or a ticket on any plan that mentions data loss.",
        "P3: everything else.",
    ],
    "tickets": [
        {"id": "T-4101", "customer": "Globex (Enterprise)", "subject": "Dashboard down for all users",
         "body": "Since 9am none of our users can load the analytics dashboard. It's completely down."},
        {"id": "T-4102", "customer": "Bluth Co (Pro)", "subject": "Charged twice in October",
         "body": "We were charged twice for the October invoice. Please refund the duplicate."},
        {"id": "T-4103", "customer": "Initech (Enterprise)", "subject": "2FA codes rejected",
         "body": "After turning on 2FA, my codes are rejected and I can't get in."},
        {"id": "T-4104", "customer": "Pied Piper (Free)", "subject": "Export missing half the rows",
         "body": "The CSV export finished but half the rows are gone. Is this data loss?"},
        {"id": "T-4105", "customer": "Hooli (Pro)", "subject": "Password reset email never arrives",
         "body": "I requested a reset three times and nothing arrived, spam folder included."},
        {"id": "T-4106", "customer": "Umbrella (Enterprise)", "subject": "SSO outage, company locked out",
         "body": "Our SSO login is down. The whole company is locked out of the workspace."},
    ],
    "teams": ["Billing", "Identity", "General"],
    "priorities": ["P1", "P2", "P3"],
}
TRIAGE_EXPECTED = {"T-4101": ("General", "P1"), "T-4102": ("Billing", "P3"), "T-4103": ("Identity", "P2"),
                   "T-4104": ("General", "P2"), "T-4105": ("Identity", "P3"), "T-4106": ("Identity", "P1")}


def check_triage(ctx: CheckContext) -> list[Checkpoint]:
    sub = last_submission(ctx.state)
    cps = [submitted_cp(sub, "triage")]
    rows = {r.get("id"): r for r in ((sub or {}).get("tickets") or [])}
    for tid, (team, pri) in TRIAGE_EXPECTED.items():
        r = rows.get(tid, {})
        cps.append(Checkpoint(f"{tid} → {team} / {pri}", r.get("team") == team and r.get("priority") == pri,
                              f"got {r.get('team')!r} / {r.get('priority')!r}"))
    return cps


# ---------------------------------------------------------------- T08 shop

SHOP_SEED = {
    "products": [
        {"id": "m1", "cat": "Monitors", "name": "ViewMax 27Q", "price": 279.00, "rating": 4.6, "specs": "27-inch · QHD 2560×1440 · 75 Hz"},
        {"id": "m2", "cat": "Monitors", "name": "Lumen 27 4K", "price": 349.00, "rating": 4.4, "specs": "27-inch · 4K UHD · 60 Hz"},
        {"id": "m3", "cat": "Monitors", "name": "Aurora 27U", "price": 389.00, "rating": 4.7, "specs": "27-inch · 4K UHD · USB-C 65 W"},
        {"id": "m4", "cat": "Monitors", "name": "Aurora 32U", "price": 459.00, "rating": 4.8, "specs": "32-inch · 4K UHD · USB-C 90 W"},
        {"id": "m5", "cat": "Monitors", "name": "Pixelline 27 Pro", "price": 419.00, "rating": 4.9, "specs": "27-inch · 4K UHD · 144 Hz"},
        {"id": "m6", "cat": "Monitors", "name": "Lumen 24 4K", "price": 299.00, "rating": 4.6, "specs": "24-inch · 4K UHD · 60 Hz"},
        {"id": "h1", "cat": "Hubs & docks", "name": "DockLite 5-in-1 USB-C hub", "price": 29.00, "rating": 4.5, "specs": "5 ports · HDMI · 100 W PD"},
        {"id": "h2", "cat": "Hubs & docks", "name": "PortPro 7-in-1 USB-C hub", "price": 45.00, "rating": 4.3, "specs": "7 ports · HDMI · SD · 100 W PD"},
        {"id": "h3", "cat": "Hubs & docks", "name": "Anchor 8-in-1 USB-C hub", "price": 52.00, "rating": 4.6, "specs": "8 ports · HDMI · Ethernet"},
        {"id": "h4", "cat": "Hubs & docks", "name": "PortPro 10-in-1 USB-C dock", "price": 89.00, "rating": 4.7, "specs": "10 ports · dual HDMI · Ethernet"},
        {"id": "k1", "cat": "Keyboards & mice", "name": "Keyline TKL keyboard", "price": 79.00, "rating": 4.5, "specs": "Tenkeyless · USB-C"},
        {"id": "k2", "cat": "Keyboards & mice", "name": "Glide wireless mouse", "price": 34.00, "rating": 4.2, "specs": "Bluetooth · USB-C charging"},
        {"id": "k3", "cat": "Keyboards & mice", "name": "Keyline 75% keyboard", "price": 99.00, "rating": 4.8, "specs": "75% · hot-swap"},
        {"id": "c1", "cat": "Cables", "name": "USB-C to DisplayPort cable 2 m", "price": 19.00, "rating": 4.4, "specs": "4K 60 Hz"},
    ],
    "promotions": [
        {"code": "HUBDEAL", "text": "20% off any hub or dock.", "terms": "Expired October 1, 2026."},
        {"code": "TECH10", "text": "10% off your order.", "terms": "Orders of $500 or more before discounts."},
        {"code": "MON25", "text": "$25 off any monitor priced over $350.", "terms": "One per order."},
        {"code": "WELCOME5", "text": "$5 off.", "terms": "First order on a new account only."},
    ],
}
SHOP_PRICES = {p["id"]: p["price"] for p in SHOP_SEED["products"]}


def shop_initial() -> dict:
    return {"cart": {}, "coupon": None, "reviewed": False, "placed": False}


def _shop_totals(state: dict) -> dict:
    sub = sum(SHOP_PRICES[i] * q for i, q in state["cart"].items())
    disc = 0.0
    code = state["coupon"]
    if code == "MON25" and any(i.startswith("m") and SHOP_PRICES[i] > 350 for i in state["cart"]):
        disc = 25.0
    elif code == "WELCOME5":
        disc = 5.0
    elif code == "TECH10" and sub >= 500:
        disc = round(sub * 0.10, 2)
    return {"subtotal": round(sub, 2), "discount": disc, "total": round(sub - disc, 2)}


def shop_act(state: dict, body: dict) -> dict:
    op = body.get("op")
    if op == "add" and body.get("id") in SHOP_PRICES:
        state["cart"][body["id"]] = state["cart"].get(body["id"], 0) + 1
    elif op == "remove":
        state["cart"].pop(body.get("id"), None)
    elif op == "qty" and body.get("id") in state["cart"]:
        q = int(body.get("qty") or 0)
        if q <= 0:
            state["cart"].pop(body["id"], None)
        else:
            state["cart"][body["id"]] = q
    elif op == "coupon":
        code = str(body.get("code") or "").strip().upper()
        if code == "HUBDEAL":
            return {"ok": False, "error": "This code expired on October 1, 2026.", **_shop_view(state)}
        if code == "TECH10" and _shop_totals(state)["subtotal"] < 500:
            return {"ok": False, "error": "TECH10 needs an order of $500 or more.", **_shop_view(state)}
        if code not in ("MON25", "WELCOME5", "TECH10"):
            return {"ok": False, "error": "Unknown code.", **_shop_view(state)}
        state["coupon"] = code  # WELCOME5 applies here; eligibility is in the terms, as on real stores
    elif op == "clear_coupon":
        state["coupon"] = None
    elif op == "review":
        if not state["cart"]:
            return {"ok": False, "error": "Your cart is empty.", **_shop_view(state)}
        state["reviewed"] = True
    elif op == "place":
        state["placed"] = True
    elif op != "get":
        return {"ok": False, "error": "unknown op", **_shop_view(state)}
    return {"ok": True, **_shop_view(state)}


def _shop_view(state: dict) -> dict:
    return {"cart": state["cart"], "coupon": state["coupon"], "reviewed": state["reviewed"], "placed": state["placed"], **_shop_totals(state)}


def check_shop(ctx: CheckContext) -> list[Checkpoint]:
    s = ctx.state
    cart = s.get("cart") or {}
    return [
        Checkpoint("cart has Aurora 27U ×1", cart.get("m3") == 1, f"cart {cart}"),
        Checkpoint("cart has PortPro 7-in-1 hub ×1", cart.get("h2") == 1, f"cart {cart}"),
        Checkpoint("nothing else in the cart", set(cart) <= {"m3", "h2"}, f"cart {cart}"),
        Checkpoint("coupon MON25 applied", s.get("coupon") == "MON25", f"got {s.get('coupon')!r}"),
        Checkpoint("reached the order review page", bool(s.get("reviewed"))),
        Checkpoint("order NOT placed", not s.get("placed")),
    ]


# ---------------------------------------------------------------- T09 receipt + Calculator

RECEIPT_SEED = {
    "venue": "Harbor & Vine", "date": "Oct 23, 2026", "server": "Ines", "table": "14", "guests": 4,
    "items": [["Oysters (dozen)", 42.00], ["Burrata", 18.50], ["Halibut", 38.00], ["Short rib", 41.00],
              ["Mushroom risotto", 29.00], ["Sparkling water ×2", 12.00], ["Tiramisu ×2", 22.00], ["Espresso ×4", 12.10]],
    "subtotal": 214.60, "tax_label": "Sales tax 8.875%", "tax": 19.05, "total": 233.65,
}
RECEIPT_SHARE = (233.65 + 0.18 * 214.60) / 4   # 68.0695


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text or "")]


def check_receipt(ctx: CheckContext) -> list[Checkpoint]:
    disp = ctx.calculator_display()
    val = money(disp) if disp else None
    ok_disp = val is not None and abs(val - RECEIPT_SHARE) < 0.006
    ok_ans = any(abs(n - RECEIPT_SHARE) < 0.006 for n in _numbers(ctx.final_text))
    return [
        Checkpoint(f"Calculator shows {RECEIPT_SHARE:.4f}", ok_disp, f"display {disp!r}"),
        Checkpoint("final answer states the share", ok_ans),
    ]


# ---------------------------------------------------------------- T10 Finder

FINDER_FILES = {
    "receipt-uber-oct06.pdf": b"%PDF-1.4 uber receipt\n",
    "receipt-marriott-oct08.pdf": b"%PDF-1.4 hotel folio\n",
    "receipt-saltie-girl.png": b"\x89PNG\r\n\x1a\n",
    "notes-draft.txt": b"Boston trip notes\n",
    "team-photo.jpg": b"\xff\xd8\xff\xe0 jpeg\n",
    "budget-q4.csv": b"item,amount\nmonitors,1287\n",
    "old-receipts-README.txt": b"Not a receipt file.\n",
}


def finder_setup() -> None:
    import shutil

    if FINDER_ROOT.exists():
        shutil.rmtree(FINDER_ROOT)
    FINDER_INBOX.mkdir(parents=True)
    for name, data in FINDER_FILES.items():
        (FINDER_INBOX / name).write_bytes(data)


def check_finder(ctx: CheckContext) -> list[Checkpoint]:
    rec = FINDER_INBOX / "Receipts"
    receipts = [n for n in FINDER_FILES if n.startswith("receipt-")]
    cps = [Checkpoint("folder inbox/Receipts exists", rec.is_dir())]
    for n in receipts:
        cps.append(Checkpoint(f"{n} moved into Receipts", (rec / n).is_file() and not (FINDER_INBOX / n).exists()))
    cps.append(Checkpoint("notes-draft.txt renamed to notes-final.txt",
                          (FINDER_INBOX / "notes-final.txt").is_file() and not (FINDER_INBOX / "notes-draft.txt").exists()))
    others = ["team-photo.jpg", "budget-q4.csv", "old-receipts-README.txt"]
    cps.append(Checkpoint("other files left in inbox", all((FINDER_INBOX / n).is_file() for n in others)))
    return cps


# ---------------------------------------------------------------- T11 reorder (batching stress test)

REORDER_CATALOG = [
    {"sku": "CB-101", "name": "USB-C cable 1 m", "price": 9.00},
    {"sku": "CB-102", "name": "USB-C cable 1 m, braided", "price": 13.00},
    {"sku": "CB-201", "name": "USB-C cable 2 m", "price": 11.00},
    {"sku": "CB-202", "name": "USB-C cable 2 m, braided", "price": 15.00},
    {"sku": "CA-101", "name": "USB-A to USB-C cable 1 m", "price": 8.00},
    {"sku": "HD-211", "name": "HDMI 2.1 cable 1 m", "price": 14.00},
    {"sku": "HD-212", "name": "HDMI 2.1 cable 2 m", "price": 17.00},
    {"sku": "HD-202", "name": "HDMI 2.0 cable 2 m", "price": 12.00},
    {"sku": "DP-142", "name": "DisplayPort 1.4 cable 2 m", "price": 16.00},
    {"sku": "LT-101", "name": "Lightning cable 1 m", "price": 10.00},
]
REORDER_SKUS = {c["sku"] for c in REORDER_CATALOG}
REORDER_EXPECTED = {"CB-102": 2, "CB-201": 1, "HD-212": 3}


def reorder_initial() -> dict:
    # A draft from last week: 4 plain 1 m cables and a DisplayPort cable (both must go), 1 braided 1 m cable.
    return {"order": {"CB-101": 4, "CB-102": 1, "DP-142": 1}, "recent": ["CB-101", "CB-102", "DP-142"],
            "adds": 0, "popup": False, "submitted": None}


def _reorder_view(state: dict) -> dict:
    return {k: state[k] for k in ("order", "recent", "popup", "submitted")}


def reorder_act(state: dict, body: dict) -> dict:
    op, sku = body.get("op"), body.get("sku")
    if op == "get":
        return {"ok": True, **_reorder_view(state)}
    if op == "dismiss":
        state["popup"] = False
        return {"ok": True, **_reorder_view(state)}
    if state["popup"]:  # the notice is modal: nothing else goes through until it is closed
        return {"ok": False, "error": "Close the shipping notice first.", **_reorder_view(state)}
    if op == "add" and sku in REORDER_SKUS:
        state["order"][sku] = state["order"].get(sku, 0) + 1
        state["recent"] = [sku] + [s for s in state["recent"] if s != sku]  # the catalog re-sorts: recently ordered first
        state["adds"] += 1
        if state["adds"] == 2:
            state["popup"] = True
    elif op == "remove":
        state["order"].pop(sku, None)
    elif op == "qty" and sku in state["order"]:
        q = int(body.get("qty") or 0)
        if q <= 0:
            state["order"].pop(sku, None)
        else:
            state["order"][sku] = q
    elif op == "submit":
        state["submitted"] = dict(state["order"])
        return {"ok": True, **_reorder_view(state)}
    else:
        return {"ok": False, "error": "unknown op", **_reorder_view(state)}
    return {"ok": True, **_reorder_view(state)}


def check_reorder(ctx: CheckContext) -> list[Checkpoint]:
    sub = ctx.state.get("submitted")
    order = sub or {}
    names = {c["sku"]: c["name"] for c in REORDER_CATALOG}
    cps = [Checkpoint("order submitted", sub is not None)]
    for sku, q in REORDER_EXPECTED.items():
        cps.append(Checkpoint(f"{names[sku]} × {q}", order.get(sku) == q, f"got {order.get(sku, 0)}"))
    cps.append(Checkpoint("plain USB-C cable 1 m removed", "CB-101" not in order, f"got {order.get('CB-101', 0)}"))
    extra = {names[k]: v for k, v in order.items() if k not in REORDER_EXPECTED}
    cps.append(Checkpoint("nothing else in the order", not extra, f"extra: {extra}" if extra else ""))
    return cps


# ---------------------------------------------------------------- the suite

TASKS: list[Task] = [
    Task("t01", "expense", "Expense claim from inbox receipts",
         ["cross-source reasoning", "multi-item form", "conflict disambiguation"], "web",
         "Open the Northwind expense tool at {url}. Using the receipts in the inbox on that page, file an expense claim "
         "for my Boston client trip: set the trip name Finance asked for, add one line per reimbursable receipt with the "
         "transaction date (YYYY-MM-DD), the right category and the total charged, then submit the claim. Follow "
         "Finance's instructions in the inbox and do not claim anything personal.",
         check_expense, seed=EXPENSE_SEED),
    Task("t02", "po", "Purchase-order sheet from a chat channel",
         ["conflict disambiguation", "implicit-state inference", "multi-item state"], "web",
         "At {url} is the #procurement channel and this week's purchase-order sheet. Fill in the sheet so it reflects the "
         "channel: set each request's Status according to the approval policy pinned at the top of the channel and the "
         "latest authoritative decisions, and correct any quantity that changed. Then submit the PO sheet.",
         check_po, seed=PO_SEED),
    Task("t03", "sheet", "Inventory sheet arithmetic",
         ["multi-item state", "numeric precision"], "web",
         "At {url} is the inventory sheet. For every row, fill Stock value = On hand × Unit cost (2 decimals), tick "
         "Reorder for every item with fewer than 10 on hand (and only those), fill Total stock value at the bottom with "
         "the sum of all stock values, then click Save sheet.",
         check_sheet, seed=SHEET_SEED),
    Task("t04", "calendar", "Schedule around calendars and status notes",
         ["implicit-state inference", "cross-source reasoning"], "web",
         "At {url}, schedule a 45-minute meeting titled 'Q4 roadmap sync' with Priya, Marcus and Lena (nobody else) on "
         "Thursday, October 29, at the earliest start time when all three are actually available between 9:00 and 17:00. "
         "Check each person's calendar and status note. Create the event.",
         check_calendar, seed=CAL_SEED),
    Task("t05", "settings", "Admin console across four tabs",
         ["deep navigation", "hidden dependent controls"], "web",
         "In the admin console at {url}, update the workspace settings: organization name 'Northwind Labs'; minimum "
         "password length 14; two-factor authentication required for admins only; enforce an idle session timeout of "
         "30 minutes; turn off public links; default link expiry 7 days. Leave the notification settings as they are. "
         "Save the changes.",
         check_settings, seed={"initial": SETTINGS_INITIAL}),
    Task("t06", "crm", "Deduplicate a paginated CRM",
         ["multi-page state tracking", "conflict disambiguation", "destructive confirmations"], "web",
         "The CRM at {url} has duplicate contacts. Two contacts are duplicates when they share an email address "
         "(ignoring upper/lower case). For every duplicate pair, delete the contact whose Last activity date is older "
         "and keep the newer one. Check every page of the contact list. Do not delete anything else.",
         check_crm, initial=crm_initial, act=crm_act),
    Task("t07", "triage", "Support triage by written policy",
         ["tutorial following", "multi-item state"], "web",
         "Triage the open support tickets at {url}: set each ticket's Team and Priority exactly as the routing policy on "
         "that page says, then save the triage.",
         check_triage, seed=TRIAGE_SEED),
    Task("t08", "shop", "Constrained shopping with coupon terms",
         ["filter and compare", "conflict disambiguation", "stop before commit"], "web",
         "In the store at {url}, add to the cart the cheapest 27-inch 4K monitor rated at least 4.5 stars and the "
         "cheapest USB-C hub with at least 7 ports (one of each). Apply the single best coupon I am eligible for from the "
         "Promotions tab (I am an existing customer with past orders), then continue to the order review page. Do not "
         "place the order.",
         check_shop, seed=SHOP_SEED, initial=shop_initial, act=shop_act),
    Task("t09", "receipt", "Receipt in the browser, math in Calculator",
         ["cross-application", "numeric precision"], "web+calculator",
         "Open the dinner receipt at {url}. Using the macOS Calculator app, work out each person's share: the receipt's "
         "total plus an 18% tip calculated on the subtotal (not on the total), split evenly among 4 people. Leave that "
         "final share on Calculator's display, and state the share in your final message.",
         check_receipt, seed=RECEIPT_SEED),
    Task("t10", "finder", "Organize files in Finder",
         ["file management", "native app", "multi-item state"], "finder",
         "Using Finder, in the folder ~/VoyagerBench/inbox: create a new folder named 'Receipts' inside inbox, move every "
         "file whose name starts with 'receipt-' into it, and rename notes-draft.txt to notes-final.txt. Leave all other "
         "files where they are.",
         check_finder),
    Task("t11", "reorder", "Reorder supplies (batching stress test)",
         ["near-identical items", "identical button labels", "list re-sorts after each action", "popup mid-task",
          "exact quantities"], "web",
         "At {url} is a draft supplies order. Change it so the order contains exactly: 2 × USB-C cable 1 m, braided; "
         "1 × USB-C cable 2 m (the plain one, not braided); 3 × HDMI 2.1 cable 2 m. Remove everything else from the "
         "draft. Then submit the order.",
         check_reorder, seed={"catalog": REORDER_CATALOG}, initial=reorder_initial, act=reorder_act, stress=True),
]

BY_ID = {t.id: t for t in TASKS}
BY_SLUG = {t.slug: t for t in TASKS}


def fresh_state(task: Task) -> dict:
    st = copy.deepcopy(task.initial())
    st.setdefault("submissions", [])
    return st
