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
