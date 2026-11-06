from datetime import date
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
