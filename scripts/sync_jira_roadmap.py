#!/usr/bin/env python3
"""
Sync Jira Epic ↔ GitHub Issue + Project board.

Modes (set JIRA_ISSUE_KEY env var):
  - Single Epic: sync or unpublish only that key (Jira webhook / repository_dispatch).
  - Empty key:   full JQL reconcile (manual workflow_dispatch or optional cron).

Publish: Epic must match jira.jql (Ready checked + issuetype Epic, etc.).
Unpublish: Epic no longer matches JQL → close GitHub issue if one exists.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

import requests
import yaml

GITHUB_API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"


def load_config() -> dict:
    path = os.environ.get("ROADMAP_SYNC_CONFIG", "roadmap-sync.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def jira_auth() -> dict[str, str]:
    raw = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()
    return {
        "Authorization": "Basic " + base64.b64encode(raw).decode(),
        "Accept": "application/json",
    }


def github_auth() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ["ROADMAP_SYNC_GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def jira_field_ids(cfg: dict) -> list[str]:
    return list(cfg["jira"]["fields"].values())


def cf(issue: dict, key: str, cfg: dict) -> Any:
    fid = cfg["jira"]["fields"].get(key)
    if not fid:
        return None
    return issue.get("fields", {}).get(fid)


def jira_search_jql(jql: str, cfg: dict) -> list[dict]:
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    fields = jira_field_ids(cfg) + ["issuetype"]
    url = f"{base}/rest/api/3/search/jql"
    print(f"Jira JQL: {jql}")
    out: list[dict] = []
    next_page_token: str | None = None
    while True:
        body: dict[str, Any] = {"jql": jql, "maxResults": 50, "fields": fields}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        r = requests.post(
            url,
            headers={**jira_auth(), "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        print(f"Jira JQL results: {json.dumps(data, indent=2)}")
        batch = data.get("issues", [])
        out.extend(batch)
        if data.get("isLast", True) or not batch:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return out


def jira_fetch_issue(key: str, cfg: dict) -> dict | None:
    """Load one issue; None if missing."""
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    fields = jira_field_ids(cfg) + ["issuetype"]
    r = requests.get(
        f"{base}/rest/api/3/issue/{key}",
        headers=jira_auth(),
        params={"fields": ",".join(fields)},
        timeout=60,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def epic_publish_jql(cfg: dict, key: str | None = None) -> str:
    base = cfg["jira"]["jql"].strip()
    if key:
        return f'key = "{key}" AND ({base})'
    return base


def is_epic(issue: dict) -> bool:
    it = issue.get("fields", {}).get("issuetype") or {}
    name = (it.get("name") or "").lower()
    return name == "epic"


def jira_label(key: str) -> str:
    return f"jira-{key.lower()}"


def list_github_issues(
    repo: str, labels: list[str] | None = None, state: str = "open"
) -> list[dict]:
    """List repo issues by label via Issues API (avoids Search API 422/auth quirks)."""
    url = f"{GITHUB_API}/repos/{repo}/issues"
    params: dict[str, Any] = {"state": state, "per_page": 100}
    if labels:
        params["labels"] = ",".join(labels)
    out: list[dict] = []
    while url:
        r = requests.get(
            url,
            headers=github_auth(),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        out.extend(issue for issue in batch if "pull_request" not in issue)
        url = None
        params = None
        for part in r.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part[part.index("<") + 1 : part.index(">")]
                break
    return out


def find_github_issue(repo: str, jira_key: str) -> dict | None:
    issues = list_github_issues(repo, labels=[jira_label(jira_key)], state="all")
    return issues[0] if issues else None


def close_github_issue(repo: str, jira_key: str) -> bool:
    existing = find_github_issue(repo, jira_key)
    if not existing or existing.get("state") == "closed":
        return False
    r = requests.patch(
        f"{GITHUB_API}/repos/{repo}/issues/{existing['number']}",
        headers=github_auth(),
        json={"state": "closed"},
        timeout=30,
    )
    r.raise_for_status()
    return True


def create_or_update_issue(
    repo: str, jira_key: str, title: str, body: str, labels: list[str]
) -> tuple[dict, str]:
    """Returns (issue json, action) where action is create | update | unchanged."""
    all_labels = labels + [jira_label(jira_key)]
    existing = find_github_issue(repo, jira_key)
    payload = {"title": title.strip(), "body": body.strip(), "labels": all_labels, "state": "open"}

    if existing:
        existing_labels = {x["name"] for x in existing.get("labels", [])}
        if (
            existing.get("title") == payload["title"]
            and (existing.get("body") or "") == payload["body"]
            and existing.get("state") == "open"
            and set(all_labels).issubset(existing_labels)
        ):
            return existing, "unchanged"
        r = requests.patch(
            f"{GITHUB_API}/repos/{repo}/issues/{existing['number']}",
            headers=github_auth(),
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json(), "update"

    r = requests.post(
        f"{GITHUB_API}/repos/{repo}/issues",
        headers=github_auth(),
        json={"title": payload["title"], "body": payload["body"], "labels": all_labels},
        timeout=30,
    )
    r.raise_for_status()
    return r.json(), "create"


def graphql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        GRAPHQL,
        headers=github_auth(),
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def project_id(org: str, number: int) -> str:
    data = graphql(
        "query($o:String!,$n:Int!){ organization(login:$o){ projectV2(number:$n){ id } } }",
        {"o": org, "n": number},
    )
    pid = data["organization"]["projectV2"]["id"]
    if not pid:
        raise RuntimeError(f"Project not found: {org}#{number}")
    return pid


def add_to_project(project_id: str, issue_node_id: str) -> str:
    data = graphql(
        """
        mutation($p:ID!,$c:ID!){
          addProjectV2ItemById(input:{projectId:$p,contentId:$c}){ item { id } }
        }
        """,
        {"p": project_id, "c": issue_node_id},
    )
    return data["addProjectV2ItemById"]["item"]["id"]


def option_id(project_id: str, field_id: str, name: str) -> str:
    data = graphql(
        """
        query($p:ID!){
          node(id:$p){
            ... on ProjectV2 {
              fields(first:50){
                nodes{
                  ... on ProjectV2SingleSelectField { id options { id name } }
                }
              }
            }
          }
        }
        """,
        {"p": project_id},
    )
    for field in data["node"]["fields"]["nodes"]:
        if field and field.get("id") == field_id:
            for opt in field.get("options") or []:
                if opt["name"] == name:
                    return opt["id"]
    raise RuntimeError(f"Column option {name!r} not found for field {field_id}")


def set_board_column(project_id: str, item_id: str, field_id: str, column_name: str) -> None:
    oid = option_id(project_id, field_id, column_name)
    graphql(
        """
        mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
          updateProjectV2ItemFieldValue(
            input:{ projectId:$p, itemId:$i, fieldId:$f, value:{ singleSelectOptionId:$o } }
          ){ projectV2Item { id } }
        }
        """,
        {"p": project_id, "i": item_id, "f": field_id, "o": oid},
    )


def write_jira_url(key: str, url: str, cfg: dict) -> None:
    fid = cfg["jira"]["fields"].get("github_url")
    if not fid:
        return
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    r = requests.put(
        f"{base}/rest/api/3/issue/{key}",
        headers={**jira_auth(), "Content-Type": "application/json"},
        json={"fields": {fid: url}},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"WARN: could not update Jira URL for {key}", file=sys.stderr)


def sync_epic(
    epic: dict,
    cfg: dict,
    repo: str,
    pid: str,
    col_field: str,
    col_value: str,
    labels: list[str],
) -> None:
    key = epic["key"]
    title = cf(epic, "public_title", cfg)
    body = cf(epic, "public_summary", cfg)
    if not title or not body:
        print(f"UNPUBLISH {key}: missing Public Roadmap Title or Summary")
        if close_github_issue(repo, key):
            print(f"  closed GitHub issue for {key}")
        return

    gh, action = create_or_update_issue(repo, key, str(title), str(body), labels)
    print(f"{action.upper()} #{gh['number']} ← {key}")
    if action == "create":
        item_id = add_to_project(pid, gh["node_id"])
        set_board_column(pid, item_id, col_field, col_value)
        print(f"  → Project column: {col_value}")

    write_jira_url(key, gh["html_url"], cfg)


def sync_single_epic(key: str, cfg: dict, repo: str, pid: str, col_field: str, col_value: str, labels: list[str]) -> None:
    key = key.strip().upper()
    print(f"Single-epic mode: {key}")

    issue = jira_fetch_issue(key, cfg)
    if not issue:
        print(f"WARN: {key} not found in Jira")
        if close_github_issue(repo, key):
            print(f"  closed GitHub issue for {key}")
        return

    if not is_epic(issue):
        print(f"SKIP {key}: not an Epic (issuetype={issue['fields']['issuetype'].get('name')})")
        return

    published = jira_search_jql(epic_publish_jql(cfg, key), cfg)
    if not published:
        print(f"UNPUBLISH {key}: does not match publish JQL (Ready unchecked or filters)")
        if close_github_issue(repo, key):
            print(f"  closed GitHub issue for {key}")
        return

    sync_epic(published[0], cfg, repo, pid, col_field, col_value, labels)


def sync_all(cfg: dict, repo: str, pid: str, col_field: str, col_value: str, labels: list[str]) -> None:
    jql = epic_publish_jql(cfg, None)
    epics = jira_search_jql(jql, cfg)
    print(f"Full reconcile mode: {len(epics)} epic(s) match JQL")

    published_keys = {e["key"] for e in epics}
    for epic in epics:
        sync_epic(epic, cfg, repo, pid, col_field, col_value, labels)

    # Close GitHub issues for epics that were published before but no longer match JQL
    for item in list_github_issues(repo, labels=labels, state="open"):
        jira_keys = [lb["name"][5:] for lb in item.get("labels", []) if lb["name"].startswith("jira-")]
        if not jira_keys:
            continue
        jk = jira_keys[0].upper()
        if jk not in published_keys:
            print(f"UNPUBLISH {jk}: no longer in JQL set")
            close_github_issue(repo, jk)


def main() -> int:
    cfg = load_config()
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ["ROADMAP_GITHUB_REPO"]
    proj = cfg["github"]["project"]
    org, num = proj["organization"], int(proj["number"])
    col_field = proj["column_field_id"]
    col_value = proj["column_value"]
    labels = list(cfg["github"].get("default_labels") or [])

    pid = project_id(org, num)
    issue_key = (os.environ.get("JIRA_ISSUE_KEY") or "").strip()

    if issue_key:
        sync_single_epic(issue_key, cfg, repo, pid, col_field, col_value, labels)
    else:
        sync_all(cfg, repo, pid, col_field, col_value, labels)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
