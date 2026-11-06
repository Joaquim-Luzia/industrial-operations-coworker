"""Pure functions: classify tickets and build Slack messages. No network here, so it's testable."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

BLOCKED = {"On Hold", "Failed Test", "Blocked"}


def workdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def prev_workday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def local_date(iso: str, tz) -> date:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).date()


@dataclass
class Attention:
    late: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    today: list = field(default_factory=list)
    week: list = field(default_factory=list)
    quiet: list = field(default_factory=list)
    undated: list = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.late or self.blocked or self.today or self.week or self.quiet or self.undated)


def classify(issues: list[dict], today: date, tz, stale_workdays: int = 3) -> Attention:
    """Each ticket lands in one bucket only, most urgent first, mirroring the board's 'Needs attention'."""
    a = Attention()
    week_end = today + timedelta(days=7)
    for i in issues:
        st = i["state"]["type"]
        if st in ("completed", "canceled", "duplicate", "backlog"):
            continue
        due = date.fromisoformat(i["dueDate"]) if i.get("dueDate") else None
        blocked = i["state"]["name"] in BLOCKED
        quiet = st == "started" and not blocked and workdays_between(local_date(i["updatedAt"], tz), today) >= stale_workdays
        if blocked:
            a.blocked.append(i)
        elif due and due < today:
            i["_late"] = (today - due).days
            a.late.append(i)
        elif due == today:
            a.today.append(i)
        elif quiet:
            i["_quiet"] = workdays_between(local_date(i["updatedAt"], tz), today)
            a.quiet.append(i)
        elif due and due <= week_end and st == "started":
            a.week.append(i)
        elif st == "started" and not due:
            a.undated.append(i)
    a.late.sort(key=lambda i: -i["_late"])
    a.quiet.sort(key=lambda i: -i["_quiet"])
    a.week.sort(key=lambda i: i["dueDate"])
    return a


def _link(i: dict) -> str:
    title = i["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(title) > 70:
        title = title[:69] + "…"
    return f"<{i['url']}|{i['identifier']}> {title}"


def _section(title: str, lines: list[str], max_lines: int = 6) -> list[dict]:
    if not lines:
        return []
    shown = lines[:max_lines] + ([f"_+{len(lines) - max_lines} more on the board_"] if len(lines) > max_lines else [])
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join("• " + l for l in shown)}}]


def headline(first: str, a: Attention, waiting: int = 0) -> str:
    parts = []
    if a.late: parts.append(f"{len(a.late)} late")
    if a.blocked: parts.append(f"{len(a.blocked)} blocked")
    if a.quiet: parts.append(f"{len(a.quiet)} gone quiet")
    if waiting: parts.append(f"{waiting} waiting on you")
    if not parts:
        return f"Morning {first}. Nothing late, blocked or quiet."
    s = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return f"Morning {first}. {s}."


def daily_blocks(person: dict, a: Attention, *, board_url: str, slug: str, reviews: list[dict] | None = None,
                 lead_extra: dict | None = None) -> tuple[str, list[dict]]:
    first = person["name"].split()[0]
    reviews = reviews or []
    waiting = 0
    if lead_extra:
        waiting = len(lead_extra.get("decisions", [])) + len(lead_extra.get("mentions", [])) + len(lead_extra.get("slack", []))
    waiting += len(reviews)
    text = headline(first, a, waiting)
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}}]
    if lead_extra:
        w = [f"Decision: {_link(i)}" for i in lead_extra.get("decisions", [])]
        w += [f"Linear mention: <{m['url']}|{m['title']}>" for m in lead_extra.get("mentions", [])]
        w += [f"Slack, {m['age']} unanswered: <{m['permalink']}|{m['who']} in {m['where']}>" for m in lead_extra.get("slack", [])]
        blocks += _section("Waiting on you", w)
    blocks += _section("Reviews waiting on you", [f"<{p['html_url']}|{p['repo']}#{p['number']}> {p['title'][:70]} · open {p['age']}d" for p in reviews])
    blocks += _section("Blocked", [f"{_link(i)} · {i['state']['name']}" for i in a.blocked])
    blocks += _section("Late", [f"{_link(i)} · {i['_late']}d late" for i in a.late])
    blocks += _section("Due today", [_link(i) for i in a.today])
    blocks += _section("Gone quiet (no update in 3+ working days)", [f"{_link(i)} · {i['_quiet']} working days" for i in a.quiet])
    blocks += _section("Due this week", [f"{_link(i)} · {date.fromisoformat(i['dueDate']).strftime('%a %d %b')}" for i in a.week])
    blocks += _section("In flight without a due date", [_link(i) for i in a.undated])
    if lead_extra and lead_extra.get("team_line"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": lead_extra["team_line"]}]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{board_url}#{slug}|Open your board> · sent by the Industrial Operations Coworker"}]})
    return text, blocks


def slug(name: str) -> str:
    import unicodedata, re
    s = unicodedata.normalize("NFD", name.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ---------------- Friday summary: same rules as the board's Week tab ----------------
def points(i: dict) -> float:
    """Unestimated tickets count as half a point, like the board."""
    e = i.get("estimate")
    return float(e) if e else 0.5


def quantile(sorted_vals: list[float], p: float) -> float:
    """Linear interpolation, matching d3.quantile on the board."""
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * p
    lo = int(pos); hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def full_weeks_before(monday: date, n: int) -> list[date]:
    """The n full weeks (their Mondays) before the week starting `monday`, oldest first."""
    return [monday - timedelta(days=7 * k) for k in range(n, 0, -1)]


def week_points(done: list[dict], monday: date, tz) -> float:
    end = monday + timedelta(days=7)
    return sum(points(i) for i in done if monday <= local_date(i["completedAt"], tz) < end)


def baseline(weekly: list[float]) -> dict | None:
    """Usual week = median of the 12 weeks before the last two; normal range = middle half. None if fewer than 6 active weeks."""
    hist = weekly[:12]
    if sum(1 for v in hist if v > 0) < 6:
        return None
    s = sorted(hist)
    return {"usual": quantile(s, .5), "lo": quantile(s, .25), "hi": quantile(s, .75)}


def cycle_forecast(cyc: dict, today: date, tz) -> dict | None:
    sc, cs = cyc.get("scopeHistory") or [], cyc.get("completedScopeHistory") or []
    if not sc:
        return None
    start = local_date(cyc["startsAt"], tz); end = local_date(cyc["endsAt"], tz) - timedelta(days=1)
    scope, done = sc[-1], cs[-1]
    elapsed = max(1, workdays_between(start - timedelta(days=1), min(today, end)))
    left = 0 if today >= end else workdays_between(today, end)
    pace, growth = done / elapsed, max(0, (scope - sc[0]) / elapsed)
    proj_done, proj_scope = done + pace * left, scope + growth * left
    return {"number": cyc["number"], "end": end, "done_pct": done / scope if scope else 0,
            "forecast": min(1, proj_done / proj_scope) if proj_scope else 0, "added": scope - sc[0], "committed": sc[0]}


def _short(i: dict) -> str:
    return f"<{i['url']}|{i['identifier']}>"


def weekly_text(*, monday: date, today: date, tz, team_done: list[dict], open_issues: list[dict], people: list[dict],
                person_done: dict, decisions: list[dict], cycle: dict | None, risky_projects: list[dict], week_url: str | None) -> str:
    name_of = {p["email"].lower(): p["name"] for p in people}
    owner = lambda i: ((i.get("assignee") or {}).get("email") or "").lower()
    this_week = [i for i in team_done if monday <= local_date(i["completedAt"], tz) <= today]
    pts = sum(points(i) for i in this_week)
    elapsed = max(1, min(5, workdays_between(monday - timedelta(days=1), today)))
    team_weeks = [week_points(team_done, m, tz) for m in full_weeks_before(monday, 14)]
    tb = baseline(team_weeks)
    lines = [f"*Industrial Operations · week of {monday.day} {monday:%b}*"]
    shipped = f"• Shipped *{len(this_week)} tickets, {pts:g} pt*"
    if tb:
        usual = tb["usual"] * elapsed / 5
        verdict = "ahead of" if pts >= usual * 1.1 else "in line with" if pts >= usual * 0.9 else "behind"
        shipped += f", {verdict} a usual week ({round(usual)} pt" + (f" by day {elapsed}" if elapsed < 5 else "") + ")."
    else:
        shipped += "."
    support = sum(1 for i in this_week if any(l["name"].lower() in ("itsm", "bug") for l in i["labels"]["nodes"]))
    if support:
        shipped += f" {support} of them {'was' if support == 1 else 'were'} support or bugs."
    lines.append(shipped)
    # slipped: due this week before today and missed
    due_week = [i for i in open_issues + this_week if i.get("dueDate") and monday <= date.fromisoformat(i["dueDate"]) < today]
    slipped = [i for i in due_week if (i.get("completedAt") is None) or local_date(i["completedAt"], tz) > date.fromisoformat(i["dueDate"])]
    if slipped:
        lines.append(f"• Slipped: *{len(slipped)}* missed {'its' if len(slipped) == 1 else 'their'} due date this week: " + ", ".join(_short(i) for i in slipped[:5]) + ("…" if len(slipped) > 5 else ""))
    else:
        lines.append("• Nothing slipped past its due date this week.")
    blocked = [i for i in open_issues if i["state"]["name"] in BLOCKED]
    late = [i for i in open_issues if i["state"]["name"] not in BLOCKED and i.get("dueDate") and date.fromisoformat(i["dueDate"]) < today and i["state"]["type"] in ("started", "unstarted")]
    lines.append(f"• Now: {len(late)} late, {len(blocked)} blocked.")
    if cycle:
        f = cycle_forecast(cycle, today, tz)
        if f:
            lines.append(f"• Cycle {f['number']}: {round(f['done_pct'] * 100)}% done, forecast {round(f['forecast'] * 100)}% by {f['end'].day} {f['end']:%b} ({f['added']:+g} pt added on a plan of {f['committed']:g}).")
    if decisions:
        lines.append(f"• Needs a decision: *{len(decisions)}*: " + ", ".join(_short(i) for i in decisions[:4]) + ("…" if len(decisions) > 4 else ""))
    if risky_projects:
        lines.append("• Projects reported at risk: " + ", ".join(f"<{p['url']}|{p['name']}>" + (" (off track)" if p.get("health") == "offTrack" else "") for p in risky_projects))
    # next week's capacity, each person against their own usual week
    nmon = monday + timedelta(days=7); nend = nmon + timedelta(days=7)
    over = []
    for p in people:
        e = p["email"].lower()
        b = baseline([week_points(person_done.get(e, []), m, tz) for m in full_weeks_before(monday, 14)])
        if not b:
            continue
        mine = [i for i in open_issues if owner(i) == e and i.get("dueDate")]
        sched = sum(points(i) for i in mine if date.fromisoformat(i["dueDate"]) < nend)
        if sched > b["hi"]:
            over.append(f"{p['name'].split()[0]} ({sched:g} pt scheduled, usual {round(b['usual'])})")
    lines.append("• Next week: " + ("over capacity: " + ", ".join(over) + "." if over else "nobody is scheduled beyond their usual week."))
    if week_url:
        lines.append(f"<{week_url}|Open the Week tab> for shipped and slipped per project, team health and the full plan.")
    return "\n".join(lines)
