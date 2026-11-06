"""Thin API clients: Linear (GraphQL), Slack (Web API), GitHub (REST search)."""
from __future__ import annotations
import time
import requests


class LinearClient:
    URL = "https://api.linear.app/graphql"

    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": api_key, "Content-Type": "application/json"})

    def q(self, query: str, variables: dict | None = None) -> dict:
        r = self.s.post(self.URL, json={"query": query, "variables": variables or {}}, timeout=60)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"Linear: {body['errors'][0].get('message')}")
        return body["data"]

    def paged(self, query: str, path: list[str], variables: dict, max_pages: int = 8) -> list[dict]:
        out, after = [], None
        for _ in range(max_pages):
            data = self.q(query, {**variables, "after": after})
            node = data
            for k in path:
                node = node[k]
            out.extend(node["nodes"])
            if not node["pageInfo"]["hasNextPage"]:
                break
            after = node["pageInfo"]["endCursor"]
        return out


class SlackClient:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})

    def call(self, method: str, **params) -> dict:
        for attempt in range(4):
            if method in ("chat.postMessage", "reactions.add", "conversations.open"):
                r = self.s.post(f"https://slack.com/api/{method}", json=params, timeout=30)
            else:
                r = self.s.get(f"https://slack.com/api/{method}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            body = r.json()
            if not body.get("ok"):
                if body.get("error") == "already_reacted":
                    return body
                raise RuntimeError(f"Slack {method}: {body.get('error')}")
            return body
        raise RuntimeError(f"Slack {method}: rate limited")

    def user_id(self, email: str) -> str | None:
        try:
            return self.call("users.lookupByEmail", email=email)["user"]["id"]
        except RuntimeError:
            return None

    def dm_channel(self, user_id: str) -> str:
        return self.call("conversations.open", users=user_id)["channel"]["id"]

    def channel_id(self, name: str) -> str | None:
        cursor = None
        while True:
            body = self.call("conversations.list", types="public_channel,private_channel", limit=1000, exclude_archived=True, **({"cursor": cursor} if cursor else {}))
            for c in body["channels"]:
                if c["name"] == name:
                    return c["id"]
            cursor = body.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return None


class GitHubClient:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def review_requests(self, login: str, org: str = "Procimo") -> list[dict]:
        r = self.s.get("https://api.github.com/search/issues",
                       params={"q": f"is:pr is:open review-requested:{login} org:{org} archived:false", "per_page": 20}, timeout=30)
        r.raise_for_status()
        return r.json().get("items", [])
