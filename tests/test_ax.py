from voyager.ax import candidate_criteria, candidates, describe, diff, parse_ax, parse_line


def test_parse_header_and_elements(fixture_text):
    t = parse_ax(fixture_text("dia_google.txt"))
    assert t.window_title == "Google" and t.app == "Dia"
    assert t.focused == 15
    e15 = t.get(15)
    assert e15.role == "text entry area" and e15.name == "Search" and e15.settable
    e2 = t.get(2)
    assert e2.role == "HTML content" and e2.url.startswith("google.com/webhp")
    assert t.url.startswith("google.com/") and t.host == "google.com"
    e40 = t.get(40)
    assert e40.value == "google.com / Google" and e40.id == "navigationBarAssistantBarTextField"
    e73 = t.get(73)
    assert e73.role == "full screen button" and e73.actions == ["zoom the window"] and e73.help.startswith("this button")
    e70 = t.get(70)
    assert e70.flags == {"disabled", "settable", "float"} and e70.disabled
    e10 = t.get(10)
    assert e10.name.startswith("Google Account: Jane Doe") and "Google membership" in e10.name
    assert t.get(78).role == "menu bar item" and t.get(78).name == "File"


def test_hierarchy_and_named_ancestor(fixture_text):
    t = parse_ax(fixture_text("chat_panel.txt"))
    send = t.get(12)
    assert send.parent == 8
    anc = t.named_ancestor(send)
    assert anc is not None and anc.name == "Chat panel"
    assert 'in container "Chat panel"' in describe(send, t)


def test_stable_keys_across_reads(fixture_text):
    raw = fixture_text("dia_google.txt")
    a, b = parse_ax(raw), parse_ax(raw)
    assert a.hash == b.hash
    assert all(e.key in b.by_key for e in a.elements)
    # keys are unique
    assert len({e.key for e in a.elements}) == len(a.elements)


def test_diff_reports_opened_panel(fixture_text):
    a = parse_ax(fixture_text("chat_open.txt"))
    b = parse_ax(fixture_text("chat_panel.txt"))
    d = diff(a, b)
    added = {e.name for e in d.added}
    assert {"Chat panel", "Chat with support", "Send", "Type your message"} <= added
    assert not d.removed
    assert a.hash != b.hash
    assert "+ button \"Send\"" in d.summary()


def test_candidates_exclude_disabled_and_scrollbars(fixture_text):
    t = parse_ax(fixture_text("dia_google.txt"))
    c = candidates(t)
    idx = {e.index for e in c}
    assert 21 in idx and 15 in idx and 5 in idx
    assert 70 not in idx and 43 not in idx
    crit, mapping = candidate_criteria(t, c)
    assert list(crit)[-1] == "none" and len(mapping) == len(c)
    assert any('button "Google Search"' in v for v in crit.values())


def test_password_markers(fixture_text):
    t = parse_ax(fixture_text("login.txt"))
    assert t.get(5).password_marker
    assert not t.get(4).password_marker
    assert t.get(6).password_hint and not t.get(6).password_marker


def test_parse_line_edge_cases():
    d, i, e = parse_line("\t\t12 unknown")
    assert d == 2 and i == 12 and e.role == "unknown" and e.name == ""
    d, i, e = parse_line("3 button")
    assert e.role == "button" and e.name == ""
    d, i, e = parse_line("4 link Gmail , Value: mail.google.com/mail")
    assert e.name == "Gmail" and e.value == "mail.google.com/mail"
    d, i, e = parse_line("5 text entry area (settable) Search, Help: type here, Secondary Actions: Clear, Focus")
    assert e.settable and e.help == "type here" and e.actions == ["Clear", "Focus"]


def test_description_as_label():
    d, i, e = parse_line("\t\t18 button Description: 2, ID: Two")
    assert e.name == "" and e.help == "2" and e.id == "Two"
    assert describe(e).startswith('button "2" (id Two)')


def test_url_prefers_usable_candidates_over_elided_ones():
    from voyager.ax import usable_url

    raw = (
        'Window: "Hacker News", App: Google Chrome.\n'
        "0 standard window Hacker News - Google Chrome, URL: …, Secondary Actions: Raise\n"
        "\t10 text field (settable) Address and search bar, Value: news.ycombinator.com, Placeholder: Ask Google or type a URL\n"
        "\t19 HTML content Hacker News, URL: …\n"
        "\t\t25 link Hacker News, Value: news.ycombinator.co…\n"
    )
    t = parse_ax(raw)
    assert t.url == "news.ycombinator.com" and t.host == "news.ycombinator.com"
    assert not usable_url("…") and not usable_url("news.ycombinator.co…") and usable_url("google.com/travel/flights")
    assert usable_url("https://localhost:8765/x") and not usable_url("Hacker News")
