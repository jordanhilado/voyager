
import pytest

from voyager.ax import parse_ax
from voyager.brief import Brief
from voyager.config import Settings
from voyager.plan import Plan, Step
from voyager.policy import Budget, DriverLock, KillSwitch, Policy, PolicyViolation, luhn_ok, secret_in_text


def _policy(tmp_path, **brief):
    s = Settings(data_dir=tmp_path)
    return Policy(s, Brief(name="t", **brief), KillSwitch())


def test_luhn_and_secrets():
    assert luhn_ok("4111111111111111")
    assert secret_in_text("my card is 4111 1111 1111 1111") is not None
    assert secret_in_text("4111111111111112") is None  # not Luhn valid
    assert secret_in_text("card ending 1234") is None
    assert secret_in_text("123-45-6789") is not None
    assert secret_in_text("123456", "Enter the verification code we sent") is not None
    assert secret_in_text("123456", "How much would you like?") is None


def test_password_field_refused(fixture_text, tmp_path):
    t = parse_ax(fixture_text("login.txt"))
    p = _policy(tmp_path, allowed_sites=["example.com"])
    step = Step(do="type", target="the password box", text="hunter2")
    with pytest.raises(PolicyViolation, match="password"):
        p.check_action(step, t, t.get(5))
    # plain field is fine
    p.check_action(Step(do="type", target="email", text="user@example.com"), t, t.get(4))


def test_site_allowlist(fixture_text, tmp_path):
    t = parse_ax(fixture_text("chat_panel.txt"))  # example-store.com
    ok = _policy(tmp_path, allowed_sites=["example-store.com"])
    ok.check_site(t)
    bad = _policy(tmp_path, allowed_sites=["americanexpress.com"])
    with pytest.raises(PolicyViolation, match="allowed_sites"):
        bad.check_site(t)
    # strict mode with no readable URL refuses; lenient allows
    native = parse_ax('Window: "Notes", App: Notes.\n0 standard window Notes\n\t1 button New note\n')
    with pytest.raises(PolicyViolation):
        _policy(tmp_path, allowed_sites=["x.com"]).check_site(native)
    _policy(tmp_path, allowed_sites=["x.com"], site_check="lenient").check_site(native)
    _policy(tmp_path).check_site(native)


def test_enter_is_a_commit():
    with pytest.raises(ValueError, match="commit"):
        Step(do="key", key="Return", expect="sent")
    with pytest.raises(ValueError):
        Plan(steps=[Step(do="commit", target="Send", summary="send"), Step(do="click", target="x", expect="y")])
    Plan(steps=[Step(do="type", target="box", text="hi"), Step(do="commit", key="Return", summary="send hi")])


def test_budget_and_kill_switch():
    b = Budget(max_steps=2, max_seconds=1000, max_consecutive_failures=2)
    b.step(); b.step()
    with pytest.raises(PolicyViolation, match="steps"):
        b.step()
    b.failed()
    with pytest.raises(PolicyViolation, match="consecutive"):
        b.failed()
    k = KillSwitch()
    k.check()
    k.trip()
    with pytest.raises(Exception):
        k.check()


def test_driver_lock(tmp_path):
    import os

    (tmp_path / "driver.lock").write_text(str(os.getppid()))  # a live foreign pid
    with pytest.raises(PolicyViolation, match="another driver"):
        DriverLock(tmp_path / "driver.lock").acquire()
    (tmp_path / "driver.lock").unlink()
    a = DriverLock(tmp_path / "driver.lock")
    a.acquire()
    assert (tmp_path / "driver.lock").read_text() == str(os.getpid())
    a.release()
    assert not (tmp_path / "driver.lock").exists()
    # stale lock from a dead pid is reclaimed
    (tmp_path / "driver.lock").write_text("999999")
    a.acquire(); a.release()


def test_screen_secret_guard(tmp_path):
    from voyager.policy import screen_secret

    assert screen_secret("-----BEGIN PRIVATE KEY-----\nMIIE") is not None
    assert screen_secret("AKIAABCDEFGHIJKLMNOP") is not None
    assert screen_secret("Welcome to the help center") is None
    t = parse_ax('Window: "k.pem", App: TextEdit.\n0 standard window k.pem\n\t1 text entry area (settable) Value: -----BEGIN PRIVATE KEY-----\n')
    with pytest.raises(PolicyViolation, match="private key"):
        _policy(tmp_path).check_screen(t)
