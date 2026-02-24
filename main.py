import os
import re
import json
import requests
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
JIRA_BASE_URL     = os.getenv("JIRA_BASE_URL", "https://axiscrm.atlassian.net")
JIRA_EMAIL        = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN    = os.getenv("JIRA_API_TOKEN")
BOARD_ID          = os.getenv("JIRA_BOARD_ID", "1")
ANDREJ_ID         = os.getenv("ANDREJ_ID", "712020:00983fc3-e82b-470b-b141-77804c9be677")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CONFLUENCE_BASE   = f"{JIRA_BASE_URL}/wiki"

MAX_SPRINT_POINTS  = 40
PRIORITY_ORDER     = {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5}
COMPLETED_STATUSES = {"done", "released"}
STORY_POINTS_FIELD = "customfield_10016"
REVIEWED_FIELD     = "customfield_10128"

auth    = (JIRA_EMAIL, JIRA_API_TOKEN)
headers = {"Accept": "application/json", "Content-Type": "application/json"}

DOR_DOD_TASK = '[**Definition of Ready (DoR) - Task Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Ready-(DoR))   **|**   [**Definition of Done (DoD) - Task Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Done-(DoD))'
DOR_DOD_EPIC = '[**Definition of Ready (DoR) - Epic Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Ready-(DoR))   **|**   [**Definition of Done (DoD) - Epic Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Done-(DoD))'

SUPPORTED_TYPES = {"Epic", "Task", "Bug", "Maintenance", "Spike", "Support"}

# ── Jira helpers ──────────────────────────────────────────────────────────────

def jira_get(path, params=None):
    r = requests.get(f"{JIRA_BASE_URL}{path}", auth=auth, headers=headers, params=params)
    r.raise_for_status()
    return r.json()

def jira_put(path, payload):
    r = requests.put(f"{JIRA_BASE_URL}{path}", auth=auth, headers=headers, json=payload)
    return r.status_code in (200, 204), r

def jira_post(path, payload):
    r = requests.post(f"{JIRA_BASE_URL}{path}", auth=auth, headers=headers, json=payload)
    return r.status_code in (200, 201, 204), r

def get_active_sprint():
    return jira_get(f"/rest/agile/1.0/board/{BOARD_ID}/sprint?state=active").get("values", [])

def get_future_sprints():
    sprints = jira_get(f"/rest/agile/1.0/board/{BOARD_ID}/sprint?state=future").get("values", [])
    sprints.sort(key=lambda s: s["startDate"])
    return sprints

def get_sprint_issues(sprint_id):
    return jira_get(f"/rest/agile/1.0/sprint/{sprint_id}/issue", params={"fields": "summary,priority,status", "maxResults": 200}).get("issues", [])

def get_sprint_todo_points(sprint_id):
    return sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in get_sprint_issues(sprint_id) if i["fields"]["status"]["name"] == "To Do")

def get_andrej_ready_backlog():
    jql = f'project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status = Ready AND status != Released AND assignee = "{ANDREJ_ID}" AND cf[10016] is not EMPTY'
    issues = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": "summary,priority,customfield_10016", "maxResults": 200}).get("issues", [])
    issues.sort(key=lambda i: PRIORITY_ORDER.get(i["fields"]["priority"]["name"], 999))
    return issues

def get_backlog_issues():
    return jira_get("/rest/api/3/search/jql", params={"jql": "project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status != Released AND status != Done", "fields": "summary,priority,status,customfield_10020", "maxResults": 200}).get("issues", [])

def move_issue_to_sprint(issue_key, sprint_id):
    ok, _ = jira_post(f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": [issue_key]})
    return ok

def rank_issues(issues, label):
    if len(issues) < 2:
        log.info(f"{label}: only {len(issues)} issue(s), no ranking needed.")
        return
    issues.sort(key=lambda i: PRIORITY_ORDER.get((i["fields"].get("priority") or {}).get("name", ""), 999))
    keys = [i["key"] for i in issues]
    log.info(f"{label} — ranking {len(keys)} issues")
    for idx in range(len(keys) - 2, -1, -1):
        ok, r = jira_put("/rest/agile/1.0/issue/rank", {"issues": [keys[idx]], "rankBeforeIssue": keys[idx + 1]})
        if not ok:
            log.warning(f"Failed ranking {keys[idx]}: {r.status_code}")

def next_tuesday(dt):
    days = (1 - dt.weekday()) % 7
    return dt + timedelta(days=days if days else 7)

def create_sprint(name, start, end):
    ok, r = jira_post("/rest/agile/1.0/sprint", {"name": name, "startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"), "endDate": end.strftime("%Y-%m-%dT00:00:00.000Z"), "originBoardId": int(BOARD_ID)})
    if ok:
        s = r.json()
        log.info(f"Created sprint '{name}' (id: {s['id']})")
        return s
    log.error(f"Failed to create sprint: {r.status_code} {r.text}")
    return None

def close_sprint(sid):
    ok, _ = jira_post(f"/rest/agile/1.0/sprint/{sid}", {"state": "closed"})
    return ok

def start_sprint(sprint):
    ok, _ = jira_post(f"/rest/agile/1.0/sprint/{sprint['id']}", {"state": "active", "startDate": sprint["startDate"], "endDate": sprint["endDate"]})
    return ok

def get_incomplete_issues(sprint_id):
    return [i for i in get_sprint_issues(sprint_id) if i["fields"]["status"]["name"].lower() not in COMPLETED_STATUSES]

# ── JOB 0: Sprint Lifecycle ──────────────────────────────────────────────────

def manage_sprint_lifecycle():
    sydney_tz = pytz.timezone("Australia/Sydney")
    today = datetime.now(sydney_tz).date()
    carryover = []
    for sprint in get_active_sprint():
        end = datetime.strptime(sprint["endDate"][:10], "%Y-%m-%d").date()
        if end <= today:
            incomplete = get_incomplete_issues(sprint["id"])
            if incomplete:
                carryover.extend(incomplete)
                log.info(f"Found {len(incomplete)} incomplete issue(s) in '{sprint['name']}' to carry over.")
            if close_sprint(sprint["id"]):
                log.info(f"Closed sprint '{sprint['name']}' (ended {end}).")
            else:
                log.error(f"Failed to close sprint '{sprint['name']}'.")
    if not get_active_sprint():
        future = get_future_sprints()
        if future:
            ns = future[0]
            if start_sprint(ns):
                log.info(f"Started sprint '{ns['name']}'.")
                for issue in carryover:
                    if move_issue_to_sprint(issue["key"], ns["id"]):
                        log.info(f"Carried over {issue['key']} to '{ns['name']}'.")
            else:
                log.error(f"Failed to start sprint '{ns['name']}'.")

# ── JOB 1: Sprint Runway ─────────────────────────────────────────────────────

def ensure_sprint_runway(future_sprints, required=8):
    if len(future_sprints) >= required:
        log.info(f"Sprint runway OK — {len(future_sprints)} future sprints.")
        return future_sprints
    log.info(f"Only {len(future_sprints)} future sprints. Creating up to {required}...")
    all_s = get_future_sprints() + get_active_sprint()
    all_s.sort(key=lambda s: s.get("endDate", ""))
    last_end = datetime.strptime(all_s[-1]["endDate"][:10], "%Y-%m-%d") if all_s else datetime.now()
    for _ in range(required - len(future_sprints)):
        start = next_tuesday(last_end + timedelta(days=1))
        end = start + timedelta(days=13)
        name = f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"
        new = create_sprint(name, start, end)
        if new:
            future_sprints.append(new)
        last_end = end
    future_sprints.sort(key=lambda s: s["startDate"])
    return future_sprints

# ── ADF conversion ────────────────────────────────────────────────────────────

def markdown_to_adf(md_text):
    content = []
    for line in md_text.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        nodes = parse_inline_marks(line)
        content.append({"type": "paragraph", "content": nodes})
    return content

def parse_inline_marks(text):
    nodes = []
    pattern = r'(\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\)|[^*\[]+)'
    for m in re.finditer(pattern, text):
        if m.group(2):
            nodes.append({"type": "text", "text": m.group(2), "marks": [{"type": "strong"}]})
        elif m.group(3) and m.group(4):
            nodes.append({"type": "text", "text": m.group(3), "marks": [{"type": "link", "attrs": {"href": m.group(4)}}]})
        else:
            txt = m.group(0)
            if txt.strip():
                nodes.append({"type": "text", "text": txt})
    if not nodes:
        nodes.append({"type": "text", "text": text})
    return nodes

# ══════════════════════════════════════════════════════════════════════════════
# JOB 5: AI-Powered Ticket Enrichment
# ══════════════════════════════════════════════════════════════════════════════

def get_unreviewed_issues():
    jql = 'project = AX AND Reviewed is EMPTY AND status not in (Done, Released) ORDER BY rank ASC'
    field_list = f"summary,description,issuetype,priority,status,parent,issuelinks,attachment,{STORY_POINTS_FIELD},{REVIEWED_FIELD},sprint"
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": field_list, "maxResults": 50, "startAt": start_at})
        batch = data.get("issues", [])
        issues.extend(batch)
        if start_at + len(batch) >= data.get("total", 0):
            break
        start_at += len(batch)
    return issues


def adf_to_text(node):
    """Recursively extract plain text from an ADF node (dict or list)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(adf_to_text(n) for n in node)
    if isinstance(node, dict):
        parts = []
        if "text" in node:
            parts.append(node["text"])
        if "attrs" in node and "href" in node.get("attrs", {}):
            parts.append(node["attrs"]["href"])
        if "marks" in node:
            for m in node["marks"]:
                if m.get("type") == "link" and "attrs" in m:
                    parts.append(m["attrs"].get("href", ""))
        for child in node.get("content", []):
            parts.append(adf_to_text(child))
        return " ".join(parts)
    return ""


def fetch_linked_content(issue):
    parts = []
    raw_desc = issue["fields"].get("description") or ""
    desc = adf_to_text(raw_desc) if isinstance(raw_desc, dict) else raw_desc

    page_ids = set()
    for url in re.findall(r'https?://axiscrm\.atlassian\.net/wiki/\S+', desc):
        m = re.search(r'/pages/(\d+)', url)
        if m and m.group(1) != "91062273":
            page_ids.add(m.group(1))

    for link in issue["fields"].get("issuelinks") or []:
        for d in ("inwardIssue", "outwardIssue"):
            linked = link.get(d)
            if not linked:
                continue
            linked_key = linked.get("key", "")
            linked_summary = linked.get("fields", {}).get("summary", "")
            linked_type = linked.get("fields", {}).get("issuetype", {}).get("name", "")

            # For Idea issues, fetch full details including description
            if linked_type == "Idea":
                try:
                    idea = jira_get(f"/rest/api/3/issue/{linked_key}", params={"fields": "summary,description,customfield_10016,status,priority"})
                    idea_desc = idea.get("fields", {}).get("description") or ""
                    if isinstance(idea_desc, dict):
                        idea_desc = adf_to_text(idea_desc)
                    parts.append(f"Linked Idea {linked_key}: {linked_summary}\nIdea description: {idea_desc[:4000]}")
                except Exception as e:
                    log.warning(f"Failed to fetch Idea {linked_key}: {e}")
                    parts.append(f"Linked Idea {linked_key}: {linked_summary}")
            else:
                parts.append(f"Linked issue {linked_key}: {linked_summary}")

    for pid in page_ids:
        try:
            r = requests.get(f"{CONFLUENCE_BASE}/api/v2/pages/{pid}?body-format=atlas_doc_format", auth=auth, headers=headers, timeout=10)
            if r.status_code == 200:
                page = r.json()
                body = page.get("body", {}).get("atlas_doc_format", {}).get("value", "")
                if body:
                    parts.append(f"Confluence page '{page.get('title', '')}': {body[:3000]}")
        except Exception as e:
            log.warning(f"Failed to fetch Confluence page {pid}: {e}")
    return "\n\n".join(parts)


def search_confluence_for_context(summary):
    try:
        r = requests.get(f"{CONFLUENCE_BASE}/rest/api/search", auth=auth, headers=headers, timeout=10,
            params={"cql": f'type = page AND space = "CAD" AND text ~ "{summary[:60]}"', "limit": 3})
        if r.status_code == 200:
            return "\n".join(f"- {res['title']}: {res.get('excerpt', '')[:400]}" for res in r.json().get("results", []))
    except Exception:
        pass
    return ""


def call_claude(prompt, max_tokens=2048):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        log.error(f"Claude API error: {r.status_code} {r.text[:300]}")
    except Exception as e:
        log.error(f"Claude API exception: {e}")
    return None


def build_enrichment_prompt(issue, linked_content, confluence_context, issue_type):
    f = issue["fields"]
    summary = f["summary"]
    desc = f.get("description") or ""
    if isinstance(desc, dict):
        desc = adf_to_text(desc)
    priority = (f.get("priority") or {}).get("name", "Medium")
    parent_summary = (f.get("parent") or {}).get("fields", {}).get("summary", "")
    sp = f.get(STORY_POINTS_FIELD)
    status = (f.get("status") or {}).get("name", "")

    clean_desc = re.sub(r'\[?\*?\*?Definition of (Ready|Done).*$', '', desc, flags=re.DOTALL).strip()
    clean_desc = re.sub(r'!\[.*?\]\(blob:.*?\)', '[image attached]', clean_desc)

    ctx = ""
    if linked_content:
        ctx += f"\nLINKED CONTENT:\n{linked_content}\n"
    if confluence_context:
        ctx += f"\nRELATED CONFLUENCE PAGES:\n{confluence_context}\n"

    base = f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.
The CRM serves multiple divisions: LIP (lead intake & processing) team, services team, and advisers.

You are enriching a Jira {issue_type} ticket. Fill in ONLY the Product Manager section.
Leave Engineer section fields empty — engineers fill those during refinement.

TICKET: {issue["key"]}
CURRENT SUMMARY: {summary}
PARENT EPIC: {parent_summary or 'None'}
PRIORITY: {priority}
STATUS: {status}
CURRENT STORY POINTS: {sp or 'Not set'}
EXISTING DESCRIPTION:
{clean_desc}
{ctx}
"""

    if issue_type == "Epic":
        base += """
RULES:
- Look through the LINKED CONTENT and EXISTING DESCRIPTION for an "Idea" ticket or Confluence page.
- From the linked Idea/content, extract: whether it is validated (Yes/No), a RICE score, and a PRD link.
- If a PRD URL exists in the linked content, use the full URL (e.g. "https://...").
- If any field is not found, use "N/A".

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{
  "polished_summary": "<concise epic title>",
  "pm_summary": "<1-2 sentence summary of what this epic delivers and why>",
  "validated": "<Yes or No or N/A>",
  "rice_score": "<number or N/A>",
  "prd": "<full URL or N/A>"
}"""
    elif issue_type == "Task":
        base += f"""
RULES:
- polished_summary MUST be user story format: "As a [role], I want [action], so that [benefit]"
- Estimate story points: 1 (~2hrs), 2 (~4hrs), 3 (~1 day). Max 3 per ticket.
- If work exceeds 3 story points, set needs_split=true and provide split_tasks.
- Each split task must be independently shippable, <=3 story points, user story format.
- Current story points: {sp or 'Not set'}. If already set and <=3, keep them.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "polished_summary": "<user story format>",
  "pm_summary": "<1-2 sentence summary>",
  "user_story": "<As a [role], I want [action], so that [benefit]>",
  "acceptance_criteria": ["<criterion 1>", "<criterion 2>"],
  "test_plan": "1. <step>\\n2. <step>",
  "story_points": <1-3>,
  "needs_split": <true or false>,
  "split_tasks": [
    {{"summary": "<user story>", "story_points": <1-3>, "acceptance_criteria": ["..."]}}
  ]
}}"""
    elif issue_type == "Bug":
        base += f"""
RULES:
- polished_summary should clearly describe the bug.
- Estimate story points: 1 (~2hrs), 2 (~4hrs), 3 (~1 day). Max 3.
- If fix exceeds 3 story points, set needs_split=true.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "polished_summary": "<clear bug description>",
  "pm_summary": "<expected vs actual behaviour, impact>",
  "story_points": <1-3>,
  "needs_split": false,
  "split_tasks": []
}}"""
    elif issue_type == "Maintenance":
        base += f"""
RULES:
- polished_summary should describe the maintenance work clearly.
- Estimate story points: 1 (~2hrs), 2 (~4hrs), 3 (~1 day). Max 3.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "polished_summary": "<maintenance description>",
  "pm_summary": "<what maintenance is needed and why>",
  "story_points": <1-3>,
  "needs_split": false,
  "split_tasks": []
}}"""
    elif issue_type == "Spike":
        base += f"""
RULES:
- polished_summary should frame the investigation question.
- Spikes are timeboxed. Estimate story points: 1 (~2hrs), 2 (~4hrs), 3 (~1 day). Max 3.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "polished_summary": "<investigation question>",
  "pm_summary": "<what needs investigating, what decision it informs>",
  "story_points": <1-3>,
  "needs_split": false,
  "split_tasks": []
}}"""
    elif issue_type == "Support":
        base += f"""
RULES:
- polished_summary should describe the support request clearly.
- Estimate story points: 1 (~2hrs), 2 (~4hrs), 3 (~1 day). Max 3.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "polished_summary": "<support request description>",
  "pm_summary": "<what the stakeholder needs and why>",
  "story_points": <1-3>,
  "needs_split": false,
  "split_tasks": []
}}"""

    return base


def build_description_markdown(issue_type, enrichment):
    if issue_type == "Epic":
        return f"""**Product Manager:**
1. **Summary:** {enrichment.get('pm_summary', '')}
2. **Validated:** {enrichment.get('validated', 'N/A')}
3. **RICE score:** {enrichment.get('rice_score', 'N/A')}
4. **PRD:** {enrichment.get('prd', 'N/A')}

{DOR_DOD_EPIC}"""

    elif issue_type == "Task":
        ac = enrichment.get("acceptance_criteria", [])
        ac_str = "\n".join(f"- [ ] {c}" for c in ac) if ac else "- [ ] "
        return f"""**Product Manager:**
1. **Summary:** {enrichment.get('pm_summary', '')}
2. **User story:** {enrichment.get('user_story', '')}
3. **Acceptance criteria:**
{ac_str}
4. **Test plan:**
{enrichment.get('test_plan', '')}

**Engineer:**
1. **Technical plan:**
2. **Story points estimated:**
3. **Task broken down (<=3 story points or split into parts):** Yes/No

{DOR_DOD_TASK}"""

    elif issue_type == "Bug":
        return f"""**Product Manager:**
1. **Summary:** {enrichment.get('pm_summary', '')}

**Engineer:**
1. **Investigation:**

{DOR_DOD_TASK}"""

    elif issue_type == "Maintenance":
        return f"""**Product Manager:**
1. **Summary:** {enrichment.get('pm_summary', '')}

**Engineer:**
1. **Task:**

{DOR_DOD_TASK}"""

    else:  # Spike, Support
        return f"""**Product Manager:**
1. **Summary:** {enrichment.get('pm_summary', '')}

**Engineer:**
1. **Investigation:**

{DOR_DOD_TASK}"""


def update_issue_fields(issue_key, summary=None, description_md=None, story_points=None, set_reviewed=True):
    payload = {"fields": {}, "update": {}}

    if summary:
        payload["fields"]["summary"] = summary
    if story_points is not None:
        payload["fields"][STORY_POINTS_FIELD] = float(story_points)
    if set_reviewed:
        payload["fields"][REVIEWED_FIELD] = [{"value": "Yes"}]
    if description_md:
        payload["update"]["description"] = [{"set": {"version": 1, "type": "doc", "content": markdown_to_adf(description_md)}}]

    if not payload["fields"]:
        del payload["fields"]
    if not payload["update"]:
        del payload["update"]

    ok, r = jira_put(f"/rest/api/3/issue/{issue_key}", payload)
    if not ok:
        log.warning(f"Failed to update {issue_key}: {r.status_code} {r.text[:300]}")
    return ok


def create_split_ticket(original_issue, split_data, issue_type):
    f = original_issue["fields"]

    if issue_type == "Task":
        ac = split_data.get("acceptance_criteria", [])
        ac_str = "\n".join(f"- [ ] {c}" for c in ac) if ac else "- [ ] "
        desc_md = f"""**Product Manager:**
1. **Summary:** Split from {original_issue['key']}
2. **User story:** {split_data.get('summary', '')}
3. **Acceptance criteria:**
{ac_str}
4. **Test plan:**

**Engineer:**
1. **Technical plan:**
2. **Story points estimated:**
3. **Task broken down (<=3 story points or split into parts):** Yes

{DOR_DOD_TASK}"""
    else:
        desc_md = f"""**Product Manager:**
1. **Summary:** Split from {original_issue['key']} — {split_data.get('summary', '')}

**Engineer:**
1. **Investigation:**

{DOR_DOD_TASK}"""

    payload = {
        "fields": {
            "project": {"key": "AX"},
            "summary": split_data["summary"],
            "issuetype": {"name": issue_type},
            STORY_POINTS_FIELD: float(split_data.get("story_points", 2)),
            REVIEWED_FIELD: [{"value": "Yes"}],
            "description": {"version": 1, "type": "doc", "content": markdown_to_adf(desc_md)},
        }
    }

    if f.get("parent"):
        payload["fields"]["parent"] = {"key": f["parent"]["key"]}
    if f.get("assignee"):
        payload["fields"]["assignee"] = {"accountId": f["assignee"]["accountId"]}
    if f.get("priority"):
        payload["fields"]["priority"] = {"name": f["priority"]["name"]}

    ok, r = jira_post("/rest/api/3/issue", payload)
    if ok:
        new_key = r.json().get("key", "?")
        log.info(f"  Created split ticket {new_key}: {split_data['summary']} ({split_data.get('story_points', 2)}pts)")
        sprint_data = f.get("sprint")
        if sprint_data and sprint_data.get("id"):
            move_issue_to_sprint(new_key, sprint_data["id"])
        return new_key
    else:
        log.error(f"  Failed to create split: {r.status_code} {r.text[:300]}")
        return None


def enrich_ticket_descriptions():
    if not ANTHROPIC_API_KEY:
        log.info("JOB 5 skipped — ANTHROPIC_API_KEY not set.")
        return

    issues = get_unreviewed_issues()
    if not issues:
        log.info("JOB 5: No unreviewed tickets found.")
        return

    log.info(f"JOB 5: Found {len(issues)} unreviewed ticket(s) to enrich.")

    for issue in issues:
        key = issue["key"]
        f = issue["fields"]
        issue_type = f["issuetype"]["name"]
        summary = f["summary"]

        if issue_type not in SUPPORTED_TYPES:
            log.info(f"  Skipping {key} — unsupported type '{issue_type}', marking reviewed.")
            update_issue_fields(key, set_reviewed=True)
            continue

        log.info(f"  Enriching {key} ({issue_type}): {summary}")

        linked_content = fetch_linked_content(issue)
        confluence_context = search_confluence_for_context(summary)

        prompt = build_enrichment_prompt(issue, linked_content, confluence_context, issue_type)
        response = call_claude(prompt)

        if not response:
            log.warning(f"  Skipping {key} — Claude enrichment failed.")
            continue

        try:
            clean = re.sub(r'^```(?:json)?\s*', '', response)
            clean = re.sub(r'\s*```$', '', clean)
            enrichment = json.loads(clean)
        except json.JSONDecodeError as e:
            log.warning(f"  Skipping {key} — JSON parse error: {e}")
            log.debug(f"  Response: {response[:500]}")
            continue

        polished_summary = enrichment.get("polished_summary", summary)
        new_desc = build_description_markdown(issue_type, enrichment)

        new_sp = None
        if issue_type != "Epic":
            existing_sp = f.get(STORY_POINTS_FIELD)
            claude_sp = enrichment.get("story_points")
            if claude_sp is not None:
                new_sp = min(max(int(claude_sp), 1), 8)
            elif existing_sp:
                new_sp = existing_sp

        needs_split = enrichment.get("needs_split", False)
        split_tasks = enrichment.get("split_tasks", [])

        if needs_split and split_tasks and len(split_tasks) > 1:
            log.info(f"  {key} needs splitting into {len(split_tasks)} tickets.")
            created_keys = []
            for st in split_tasks:
                nk = create_split_ticket(issue, st, issue_type)
                if nk:
                    created_keys.append(nk)

            split_note = f"This ticket has been split into {len(created_keys)} smaller tickets: {', '.join(created_keys)}."
            if issue_type == "Task":
                split_desc = f"""**Product Manager:**
1. **Summary:** {split_note}
2. **User story:** See child tickets.
3. **Acceptance criteria:**
- [ ] All split tickets completed
4. **Test plan:**
Verify all split tickets pass their individual test plans.

**Engineer:**
1. **Technical plan:**
2. **Story points estimated:**
3. **Task broken down (<=3 story points or split into parts):** Yes

{DOR_DOD_TASK}"""
            elif issue_type == "Epic":
                split_desc = f"""**Product Manager:**
1. **Summary:** {split_note}
2. **Validated:** N/A
3. **RICE score:** N/A
4. **PRD:** N/A

{DOR_DOD_EPIC}"""
            else:
                split_desc = f"""**Product Manager:**
1. **Summary:** {split_note}

**Engineer:**
1. **Investigation:**

{DOR_DOD_TASK}"""

            update_issue_fields(key, summary=f"[SPLIT] {polished_summary}", description_md=split_desc,
                story_points=0 if issue_type != "Epic" else None, set_reviewed=True)
        else:
            update_issue_fields(key, summary=polished_summary, description_md=new_desc,
                story_points=new_sp, set_reviewed=True)

        log.info(f"  Completed {key}.")


# ── Main run ──────────────────────────────────────────────────────────────────

def run():
    log.info("=== Starting Jira prioritisation run ===")
    try:
        log.info("JOB 0: Sprint Lifecycle")
        manage_sprint_lifecycle()

        log.info("JOB 1: Sprint Runway")
        future_sprints = get_future_sprints()
        future_sprints = ensure_sprint_runway(future_sprints, required=8)

        log.info("JOB 2: Move Backlog to Sprints")
        backlog = get_andrej_ready_backlog()
        if not backlog:
            log.info("No READY backlog issues to move.")
        else:
            idx = 0
            for sprint in future_sprints:
                if idx >= len(backlog):
                    break
                sid, sname = sprint["id"], sprint["name"]
                avail = MAX_SPRINT_POINTS - get_sprint_todo_points(sid)
                log.info(f"Sprint '{sname}': {avail}pts available.")
                if avail <= 0:
                    continue
                while idx < len(backlog) and avail > 0:
                    issue = backlog[idx]
                    key = issue["key"]
                    pts = issue["fields"].get(STORY_POINTS_FIELD) or 0
                    pri = issue["fields"]["priority"]["name"]
                    if pts > avail:
                        idx += 1
                        continue
                    if move_issue_to_sprint(key, sid):
                        avail -= pts
                        log.info(f"Moved {key} ({pts}pts) [{pri}] to '{sname}'. {avail}pts left.")
                    idx += 1

        log.info("JOB 3: Rank All Sprints")
        for sprint in future_sprints:
            rank_issues(get_sprint_issues(sprint["id"]), f"Sprint '{sprint['name']}'")
        for sprint in get_active_sprint():
            rank_issues(get_sprint_issues(sprint["id"]), f"Active sprint '{sprint['name']}'")

        log.info("JOB 4: Rank Backlog")
        backlog_all = get_backlog_issues()
        if backlog_all:
            rank_issues(backlog_all, "Backlog")

        log.info("JOB 5: Enrich Ticket Descriptions")
        enrich_ticket_descriptions()

        log.info("=== Run complete ===")
    except Exception as e:
        log.error(f"Run failed: {e}", exc_info=True)


if __name__ == "__main__":
    sydney_tz = pytz.timezone("Australia/Sydney")
    scheduler = BlockingScheduler(timezone=sydney_tz)
    for hour, minute, name in [(7, 0, "7:00am"), (12, 0, "12:00pm"), (16, 0, "4:00pm")]:
        scheduler.add_job(run, trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=sydney_tz), id=name, name=f"{name} Sydney Run")
    log.info("Scheduler started — running at 7:00am, 12:00pm, 4:00pm AEDT Mon-Fri.")
    run()
    scheduler.start()
