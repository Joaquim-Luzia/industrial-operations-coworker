from datetime import date, timedelta
from zoneinfo import ZoneInfo
from coworker import logic

TZ = ZoneInfo("Europe/Lisbon")
TODAY = date(2026, 11, 6)  # Friday


def issue(ident, state="In Progress", stype="started", due=None, updated="2026-11-05T10:00:00Z"):
    return {"identifier": ident, "title": f"Ticket {ident}", "url": f"https://linear.app/x/{ident}", "dueDate": due,
            "updatedAt": updated, "state": {"name": state, "type": stype}}


def test_each_ticket_lands_in_one_bucket():
    a = logic.classify([
        issue("A", state="On Hold", due="2026-11-01"),                   # blocked wins over late
        issue("B", due="2026-11-03", updated="2026-10-20T09:00:00Z"),    # late wins over quiet
        issue("C", due="2026-11-06"),                                     # due today
        issue("D", updated="2026-10-30T09:00:00Z"),                       # quiet (5 working days)
        issue("E", due="2026-11-10"),                                     # this week
        issue("F"),                                                       # in flight, no date
        issue("G", stype="unstarted", due="2026-11-20"),                  # nothing to flag
        issue("H", stype="completed", due="2026-11-01"),                  # done, ignored
    ], TODAY, TZ)
    assert [i["identifier"] for i in a.blocked] == ["A"]
    assert [i["identifier"] for i in a.late] == ["B"] and a.late[0]["_late"] == 3
    assert [i["identifier"] for i in a.today] == ["C"]
    assert [i["identifier"] for i in a.quiet] == ["D"] and a.quiet[0]["_quiet"] == 5
    assert [i["identifier"] for i in a.week] == ["E"]
    assert [i["identifier"] for i in a.undated] == ["F"]


def test_workdays_skip_weekends():
    assert logic.workdays_between(date(2026, 11, 6), date(2026, 11, 9)) == 1  # Fri -> Mon
    assert logic.prev_workday(date(2026, 11, 9)) == date(2026, 11, 6)


def test_headline():
    a = logic.classify([issue("B", due="2026-11-03")], TODAY, TZ)
    assert logic.headline("Gabriel", a) == "Morning Gabriel. 1 late."
    assert logic.headline("Gabriel", logic.Attention()) == "Morning Gabriel. Nothing late, blocked or quiet."
    assert logic.headline("Joaquim", a, waiting=2) == "Morning Joaquim. 1 late and 2 waiting on you."


def test_blocks_escape_titles_and_link_board():
    i = issue("X", due="2026-11-03"); i["title"] = "Fix <script> & stuff"
    a = logic.classify([i], TODAY, TZ)
    text, blocks = logic.daily_blocks({"name": "Pedro Cardoso"}, a, board_url="https://b", slug="pedro-cardoso")
    flat = str(blocks)
    assert "&lt;script&gt; &amp; stuff" in flat and "https://b#pedro-cardoso" in flat


def test_slug_matches_board():
    assert logic.slug("João Viegas") == "joao-viegas"


def done_issue(ident, completed, est=2, due=None, email="a@x", labels=()):
    return {"identifier": ident, "title": ident, "url": f"https://l/{ident}", "estimate": est, "dueDate": due,
            "completedAt": completed, "state": {"name": "Done", "type": "completed"}, "assignee": {"email": email},
            "labels": {"nodes": [{"name": l} for l in labels]}}


def test_quantile_matches_d3():
    assert logic.quantile([1, 2, 3, 4], .5) == 2.5
    assert logic.quantile([1, 2, 3, 4], .25) == 1.75


def test_points_count_unestimated_as_half():
    assert logic.points({"estimate": None}) == 0.5 and logic.points({"estimate": 3}) == 3.0


def test_baseline_needs_six_active_weeks():
    assert logic.baseline([0] * 10 + [5] * 4) is None
    b = logic.baseline([4, 6, 8, 10, 6, 8, 4, 6, 8, 10, 6, 8, 99, 99])  # the last two weeks are ignored
    assert b["usual"] == 7.0 and b["lo"] == 6.0 and b["hi"] == 8.0


def test_weekly_text_no_ranking_and_flags_slips():
    monday = date(2026, 11, 2)
    people = [{"name": "Ana Silva", "email": "a@x"}, {"name": "Rui Costa", "email": "r@x"}]
    hist = []
    for k in range(1, 15):  # 14 full weeks of history for Ana: 10 pt a week
        d = monday - timedelta(days=7 * k) + timedelta(days=2)
        hist += [done_issue(f"H{k}a", f"{d}T10:00:00Z", 5), done_issue(f"H{k}b", f"{d}T11:00:00Z", 5)]
    this = [done_issue("W1", "2026-11-04T10:00:00Z", 5, due="2026-11-03"),            # closed late -> slipped
            done_issue("W2", "2026-11-05T10:00:00Z", 3, labels=("ITSM",))]
    open_ = [{"identifier": "O1", "title": "O1", "url": "https://l/O1", "dueDate": "2026-11-04", "estimate": 2,
              "state": {"name": "In Progress", "type": "started"}, "assignee": {"email": "a@x"}},
             {"identifier": "O2", "title": "O2", "url": "https://l/O2", "dueDate": "2026-11-10", "estimate": 20,
              "state": {"name": "Todo", "type": "unstarted"}, "assignee": {"email": "a@x"}}]
    ana = hist + this
    txt = logic.weekly_text(monday=monday, today=date(2026, 11, 6), tz=TZ, team_done=ana, open_issues=open_, people=people,
                            person_done={"a@x": ana}, decisions=[], cycle=None, risky_projects=[], week_url="https://b#week")
    assert "Shipped *2 tickets, 8 pt*" in txt and "behind a usual week (10 pt)" in txt and "1 of them was support or bugs" in txt
    assert "Slipped: *2*" in txt and "O1" in txt and "W1" in txt
    assert "over capacity: Ana (22 pt scheduled, usual 10)" in txt
    assert "Closed by" not in txt and "Rui" not in txt
