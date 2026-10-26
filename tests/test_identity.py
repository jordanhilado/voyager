from voyager.ax import Diff, Element, parse_ax
from voyager.identity import (change_summary, distinguishing_tokens, identity_mismatch, is_destructive, item_scope,
                              meaningful_change)

CATALOG = """Window: "Supplies", App: Google Chrome.
0 standard window Supplies
\t1 HTML content Supplies, URL: localhost/t/reorder
\t\t2 table
\t\t\t3 row
\t\t\t\t4 cell USB-C cable 1 m
\t\t\t\t5 cell CB-101
\t\t\t\t6 cell
\t\t\t\t\t7 button Add
\t\t\t8 row
\t\t\t\t9 cell USB-C cable 1 m, braided
\t\t\t\t10 cell CB-102
\t\t\t\t11 cell
\t\t\t\t\t12 button Add
\t\t\t13 row
\t\t\t\t14 cell HDMI 2.1 cable 1 m
\t\t\t\t15 cell HD-211
\t\t\t\t16 cell
\t\t\t\t\t17 button Add
\t\t\t18 row
\t\t\t\t19 cell HDMI 2.1 cable 2 m
\t\t\t\t20 cell HD-212
\t\t\t\t21 cell
\t\t\t\t\t22 button Add
\t\t23 article Aurora 27U
\t\t\t24 text $389.00
\t\t\t25 button Add Aurora 27U to cart
\t\t26 article Aurora 32U
\t\t\t27 text $459.00
\t\t\t28 button Add Aurora 32U to cart
\t\t29 button Submit order"""


def tree():
    return parse_ax(CATALOG)


def test_tokens():
    assert distinguishing_tokens("the 'Add' button in the 'USB-C cable 1 m, braided' row") == ["1", "add", "usb", "cable", "braided"]
    assert distinguishing_tokens("the Delete button for Sam.ONeil@globex.io") == ["Sam.ONeil@globex.io"]
    assert distinguishing_tokens("the 'Add Aurora 27U to cart' button") == ["27U", "add", "aurora", "cart"]
    # possessives and names with apostrophes are not quotes; ordinals are not identifiers
    assert distinguishing_tokens("Lena's 2nd 'Remove' button for O'Neil") == ["remove"]
    assert distinguishing_tokens("the Promotions tab") == []


def test_row_scope_tells_identical_buttons_apart():
    t = tree()
    plain, braided = t.get(7), t.get(12)
    assert item_scope(t, plain).index == 3  # the row, not the table
    target = "the 'Add' button in the 'USB-C cable 1 m, braided' row"
    assert identity_mismatch(target, t, braided) == []
    assert identity_mismatch(target, t, plain) == ["braided"]


def test_whole_numbers_only():
    t = tree()
    target = "the Add button in the HDMI 2.1 cable 2 m row"
    assert identity_mismatch(target, t, t.get(22)) == []
    assert identity_mismatch(target, t, t.get(17)) == ["2"]  # "2" in "2.1" or "HD-211" does not count


def test_near_identical_model_numbers():
    t = tree()
    assert identity_mismatch("the 'Add Aurora 27U to cart' button", t, t.get(25)) == []
    assert identity_mismatch("the 'Add Aurora 27U to cart' button", t, t.get(28)) == ["27U"]
    assert identity_mismatch("the Add button for the $389 monitor", t, t.get(25)) == []


def test_no_tokens_no_check():
    t = tree()
    assert identity_mismatch("the Submit order button", t, t.get(29)) == []
    assert identity_mismatch("anything", t, None) == []


def test_destructive():
    assert is_destructive("the 'Remove' button in the DisplayPort row")
    assert is_destructive(None, 'button "Delete contact"')
    assert is_destructive("the Place order button")
    assert not is_destructive("the Cancel button in the dialog", 'button "Cancel"')
    assert not is_destructive("the 'Add' button")


def test_meaningful_change():
    a = Element(index=1, role="button", name="Add", key="k1")
    assert not meaningful_change(None)
    assert not meaningful_change(Diff([], [], []))
    assert not meaningful_change(Diff([Element(index=2, role="group", key="k2")], [], []))  # unnamed layout only
    assert meaningful_change(Diff([Element(index=3, role="text", name="Added to order", key="k3")], [], []))
    b = Element(index=1, role="check box", name="Reorder", flags={"checked"}, key="k1")
    assert meaningful_change(Diff([], [], [(a, b)]))
    assert change_summary(Diff([], [], [])) == "no visible change"
