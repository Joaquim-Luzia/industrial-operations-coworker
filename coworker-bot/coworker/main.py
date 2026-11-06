"""Industrial Operations Coworker.

    python -m coworker.main daily     # 09:00 Lisbon, weekdays: one DM per person
    python -m coworker.main weekly    # Friday 16:00 Lisbon: summary DM to Joaquim and João

Flags:
    --force          ignore the local-time guard (the workflow runs at two UTC hours to survive DST)
    --dry-run        print what would be sent, send nothing, write nothing
    --only-to-lead   send every DM to the lead instead, prefixed with who it was for (first-run preview)
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import yaml

from .clients import LinearClient, SlackClient, GitHubClient
from . import logic

ISSUE_FIELDS = """
  identifier title url dueDate updatedAt completedAt estimate priority
  state { name type } assignee { name email } labels { nodes { name } }
  project { name } projectMilestone { name } team { name }
"""
Q_TEAM_OPEN = """query($team: String!, $after: String) {
  issues(first: 250, after: $after, filter: { team: { name: { eq: $team } }, state: { type: { in: ["started","unstarted","triage"] } } }) {
    nodes { %s } pageInfo { hasNextPage endCursor } } }""" % ISSUE_FIELDS
Q_PERSON_OPEN = """query($email: String!, $after: String) {
  issues(first: 250, after: $after, filter: { assignee: { email: { eq: $email } }, state: { type: { in: ["started","unstarted"] } } }) {
    nodes { %s } pageInfo { hasNextPage endCursor } } }""" % ISSUE_FIELDS
Q_DONE_SINCE = """query($team: String!, $since: DateTimeOrDuration!, $after: String) {
  issues(first: 250, after: $after, filter: { team: { name: { eq: $team } }, completedAt: { gte: $since } }) {
    nodes { %s } pageInfo { hasNextPage endCursor } } }""" % ISSUE_FIELDS
Q_DECISIONS = """query($label: String!, $email: String!, $after: String) {
  issues(first: 100, after: $after, filter: { labels: { name: { eq: $label } }, assignee: { email: { eq: $email } }, state: { type: { nin: ["completed","canceled"] } } }) {
    nodes { %s } pageInfo { hasNextPage endCursor } } }""" % ISSUE_FIELDS
Q_NOTIFICATIONS = """query { notifications(first: 100) { nodes {
  type readAt createdAt
  ... on IssueNotification { issue { identifier title url } actor { name } }
} } }"""
Q_TEAM_STATE = """query($id: String!) { team(id: $id) {
  activeCycle { number startsAt endsAt scopeHistory completedScopeHistory }
  projects(first: 50) { nodes { name url health targetDate status { type } } }
} }"""


def load_config() -> dict:
    with open(os.environ.get("COWORKER_CONFIG", "config.yaml")) as f:
        return yaml.safe_load(f)


def env(name: str, required: bool = True) -> str | None:
    v = os.environ.get(name)
    if required and not v:
        sys.exit(f"Missing secret {name}. See README.md.")
    return v


def local_now(cfg) -> datetime:
    return datetime.now(ZoneInfo(cfg["timezone"]))


def guard(cfg, hour: int, weekdays: set[int], force: bool) -> bool:
    now = local_now(cfg)
    if force:
        return True
    if now.weekday() not in weekdays or now.hour != hour:
        print(f"Skipping: it's {now:%a %H:%M} in {cfg['timezone']}, this job runs at {hour:02d}:00.")
        return False
    return True


def open_issues(lin: LinearClient, cfg) -> list[dict]:
    seen = {}
    for i in lin.paged(Q_TEAM_OPEN, ["issues"], {"team": cfg["linear"]["team_name"]}):
        seen[i["identifier"]] = i
    for p in cfg["people"]:
        for i in lin.paged(Q_PERSON_OPEN, ["issues"], {"email": p["email"]}, max_pages=2):
            if (i.get("team") or {}).get("name") not in cfg["linear"]["exclude_teams"]:
                seen.setdefault(i["identifier"], i)
    return list(seen.values())


def owner_email(i: dict) -> str | None:
    return ((i.get("assignee") or {}).get("email") or "").lower() or None


# ---------- Slack: unanswered mentions and DMs for the lead (needs the lead's user token) ----------
def slack_unanswered(user_token: str | None, lead_uid: str | None, now: datetime) -> list[dict]:
    if not user_token or not lead_uid:
        return []
    us = SlackClient(user_token)
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    out, seen = [], set()
    for query in (f"<@{lead_uid}> after:{since}", f"to:me after:{since}"):
        try:
            res = us.call("search.messages", query=query, count=40, sort="timestamp")
        except RuntimeError as e:
            print("Slack search skipped:", e); return out
        for m in res.get("messages", {}).get("matches", []):
            if m.get("user") == lead_uid or m.get("ts") in seen:
                continue
            seen.add(m["ts"])
            ts = float(m["ts"]); age_h = (now.timestamp() - ts) / 3600
            if age_h < 24:
                continue
            ch = m["channel"]["id"]
            thread = None
            try:
                link = m.get("permalink", "")
                if "thread_ts=" in link:
                    thread = link.split("thread_ts=")[1].split("&")[0]
                if thread:
                    later = us.call("conversations.replies", channel=ch, ts=thread, oldest=m["ts"], limit=100).get("messages", [])
                else:
                    later = us.call("conversations.history", channel=ch, oldest=m["ts"], limit=50).get("messages", [])
            except RuntimeError:
                continue
            if any(x.get("user") == lead_uid and float(x["ts"]) > ts for x in later):
                continue
            where = "a DM" if m["channel"].get("is_im") else ("#" + m["channel"].get("name", "channel"))
            out.append({"permalink": m.get("permalink"), "who": m.get("username") or "someone", "where": where,
                        "age": f"{int(age_h // 24)}d" if age_h >= 48 else f"{int(age_h)}h"})
            if len(out) >= 8:
                return out
    return out


def daily(cfg, args):
    if not guard(cfg, 9, {0, 1, 2, 3, 4}, args.force):
        return
    tz = ZoneInfo(cfg["timezone"]); now = local_now(cfg); today = now.date()
    lin = LinearClient(env("LINEAR_API_KEY")); slack = SlackClient(env("SLACK_BOT_TOKEN"))
    gh_token = env("GITHUB_TOKEN", required=False); gh = GitHubClient(gh_token) if gh_token else None
    issues = open_issues(lin, cfg)
    lead_email = cfg["lead"].lower()
    lead_uid = next((p.get("slack") for p in cfg["people"] if p["email"].lower() == lead_email and p.get("slack")), None) or slack.user_id(lead_email)
    roster = {p["email"].lower() for p in cfg["people"]}
    team = logic.classify([i for i in issues if owner_email(i) in roster], today, tz, cfg["rules"]["stale_workdays"])

    for p in cfg["people"]:
        mine = [i for i in issues if owner_email(i) == p["email"].lower()]
        a = logic.classify(mine, today, tz, cfg["rules"]["stale_workdays"])
        reviews = []
        if gh and p.get("github"):
            try:
                for pr in gh.review_requests(p["github"]):
                    opened = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                    reviews.append({"html_url": pr["html_url"], "title": pr["title"], "number": pr["number"],
                                    "repo": pr["repository_url"].rsplit("/", 1)[-1], "age": (datetime.now(timezone.utc) - opened).days})
            except Exception as e:
                print(f"GitHub reviews for {p['name']} skipped: {e}")
        lead_extra = None
        if p["email"].lower() == lead_email:
            decisions = lin.paged(Q_DECISIONS, ["issues"], {"label": cfg["linear"]["needs_decision_label"], "email": lead_email}, max_pages=1)
            mentions = []
            for n in lin.q(Q_NOTIFICATIONS)["notifications"]["nodes"]:
                if n.get("readAt") is None and "mention" in (n.get("type") or "").lower() and n.get("issue"):
                    mentions.append({"url": n["issue"]["url"], "title": f"{n['issue']['identifier']} {n['issue']['title'][:60]} ({(n.get('actor') or {}).get('name','someone')})"})
            lead_extra = {"decisions": decisions, "mentions": mentions[:8],
                          "slack": slack_unanswered(env("SLACK_LEAD_USER_TOKEN", required=False), lead_uid, now),
                          "team_line": f"Team: {len(team.late)} late, {len(team.blocked)} blocked, {len(team.quiet)} gone quiet."}
        if a.empty() and not reviews and not (lead_extra and (lead_extra["decisions"] or lead_extra["mentions"] or lead_extra["slack"])) and cfg["rules"].get("skip_empty_dms", True):
            print(f"{p['name']}: nothing to send."); continue
        text, blocks = logic.daily_blocks(p, a, board_url=cfg["board_url"], slug=logic.slug(p["name"]), reviews=reviews, lead_extra=lead_extra)
        send_dm(slack, p, text, blocks, args, lead_uid)


def send_dm(slack: SlackClient, p: dict, text: str, blocks: list[dict], args, lead_uid: str | None):
    if args.dry_run:
        print(f"\n--- DM to {p['name']} ---\n{text}\n{json.dumps(blocks, ensure_ascii=False)[:1500]}")
        return
    target = lead_uid if args.only_to_lead else (p.get("slack") or slack.user_id(p["email"]))
    if not target:
        print(f"{p['name']}: no Slack user for {p['email']}, skipped."); return
    if args.only_to_lead:
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Preview of the DM for {p['name']}_"}]}] + blocks
    slack.call("chat.postMessage", channel=slack.dm_channel(target), text=text, blocks=blocks, unfurl_links=False)
    print(f"{p['name']}: sent.")


def weekly(cfg, args):
    if not guard(cfg, 16, {4}, args.force):
        return
    tz = ZoneInfo(cfg["timezone"]); today = local_now(cfg).date()
    week_start = today - timedelta(days=today.weekday())
    lin = LinearClient(env("LINEAR_API_KEY")); slack = SlackClient(env("SLACK_BOT_TOKEN"))
    roster = {p["email"].lower() for p in cfg["people"]}
    done = [i for i in lin.paged(Q_DONE_SINCE, ["issues"], {"team": cfg["linear"]["team_name"], "since": week_start.isoformat() + "T00:00:00Z"}) if owner_email(i) in roster]
    pts = sum((i.get("estimate") or 0) for i in done)
    support = sum(1 for i in done if any(l["name"].lower() in ("itsm", "bug") for l in i["labels"]["nodes"]))
    per = Counter((i.get("assignee") or {}).get("name", "Unassigned") for i in done)
    issues = open_issues(lin, cfg)
    team = logic.classify([i for i in issues if owner_email(i) in roster], today, tz, cfg["rules"]["stale_workdays"])
    st = lin.q(Q_TEAM_STATE, {"id": cfg["linear"]["team_id"]})["team"]
    cyc = st.get("activeCycle")
    lines = [f"*Industrial Operations, week of {week_start:%d %b}*",
             f"• Closed *{len(done)} tickets* ({pts:g} points); {support} were support or bugs.",
             f"• Now *{len(team.late)} late*, *{len(team.blocked)} blocked*, {len(team.quiet)} gone quiet."]
    if cyc and cyc.get("scopeHistory"):
        sc, cs = cyc["scopeHistory"], cyc["completedScopeHistory"]
        lines.append(f"• Cycle {cyc['number']}: {cs[-1]:g} of {sc[-1]:g} points done ({round(cs[-1] / max(sc[-1], 1) * 100)}%), scope {'+' if sc[-1] >= sc[0] else ''}{sc[-1] - sc[0]:g} since it started.")
    risky = [p for p in st["projects"]["nodes"] if (p.get("status") or {}).get("type") == "started" and p.get("health") in ("atRisk", "offTrack")]
    if risky:
        lines.append("• Projects at risk: " + ", ".join(f"<{p['url']}|{p['name']}> ({'off track' if p['health'] == 'offTrack' else 'at risk'})" for p in risky))
    top = ", ".join(f"{n.split()[0]} {c}" for n, c in per.most_common(6))
    if top:
        lines.append(f"• Closed by: {top}")
    if cfg.get("weekly_report_url"):
        lines.append(f"<{cfg['weekly_report_url']}|Open the Week tab> for shipped and slipped per project, team health, risks and next week's plan.")
    text = "\n".join(lines)
    if args.dry_run:
        print(text); return
    recipients = cfg.get("weekly_recipients", [])
    if args.only_to_lead:
        lead = cfg["lead"].lower()
        recipients = [next((p.get("slack") for p in cfg["people"] if p["email"].lower() == lead and p.get("slack")), None) or slack.user_id(lead)]
        text = "_Preview of the Friday summary_\n" + text
    for uid in recipients:
        ch = slack.call("conversations.open", users=uid)["channel"]["id"]
        slack.call("chat.postMessage", channel=ch, text=text, unfurl_links=False)
    print(f"Weekly summary sent to {len(recipients)} {'person' if len(recipients) == 1 else 'people'}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["daily", "weekly"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-to-lead", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    {"daily": daily, "weekly": weekly}[args.mode](cfg, args)


if __name__ == "__main__":
    main()
