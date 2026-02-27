import os
import re
import json
import requests
import logging
from datetime import datetime, timedelta
from uuid import uuid4
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
REVIEWED_FIELD     = None  # Auto-discovered at startup

# ── AR (Strategic Roadmap / JPD) Config ───────────────────────────────────────
AR_PROJECT_KEY     = "AR"
RICE_REACH_FIELD   = "customfield_10526"
RICE_IMPACT_FIELD  = "customfield_10047"
RICE_CONFIDENCE_FIELD = "customfield_10527"
RICE_EFFORT_FIELD  = "customfield_10058"
RICE_VALUE_FIELD   = "customfield_10195"   # Formula: auto-computed by JPD
ROADMAP_FIELD      = "customfield_10560"
SWIMLANE_FIELD     = "customfield_10694"
ADVISER_FIELD      = "customfield_10696"
USER_FEEDBACK_OPTION_ID = "10575"

# Roadmap column option IDs, ordered left-to-right (highest priority first)
ROADMAP_COLUMNS = [
    {"value": "February (S2)", "id": "10232"},
    {"value": "March (S1)",    "id": "10233"},
    {"value": "March (S2)",    "id": "10269"},
    {"value": "April (S1)",    "id": "10529"},
    {"value": "April (S2)",    "id": "10530"},
    {"value": "May (S1)",      "id": "10531"},
    {"value": "May (S2)",      "id": "10538"},
    {"value": "June (S1)",     "id": "10539"},
    {"value": "June (S2)",     "id": "10540"},
    {"value": "July (S1)",     "id": "10537"},
    {"value": "July (S2)",     "id": "10541"},
]
ROADMAP_BACKLOG_ID = "10536"
ROADMAP_SHIPPED_ID = "10234"
ROADMAP_DONE_ID    = "10532"
IDEAS_PER_COLUMN   = 3  # Max ideas per roadmap column (2-3 for first, 3 for rest)

# Column index → priority rank (0 = highest priority, i.e. soonest sprint)
COLUMN_RANK = {col["id"]: idx for idx, col in enumerate(ROADMAP_COLUMNS)}
COLUMN_RANK[ROADMAP_BACKLOG_ID] = 999  # Backlog = lowest

# Populated by JOB 15: maps AX Epic key → (column_rank, rice_value)
# Used by JOB 3/4 to rank tickets by strategic priority
EPIC_ROADMAP_RANK = {}

# ── Telegram Bot Config ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")  # Auto-captured from first message if not set
JAMES_ACCOUNT_ID   = "712020:b28bb054-a469-4a9f-bfde-0b93ad1101ae"

# ── Archive Config ────────────────────────────────────────────────────────────
ARCHIVE_PROJECT_KEY = "ARU"
ARCHIVE_AGE_DAYS    = 90
# ARU only has Task, Bug, Story, Epic, Subtask — map others to Task
ARCHIVE_TYPE_MAP = {
    "Task": "Task", "Bug": "Bug", "Epic": "Epic", "Subtask": "Subtask",
    "Spike": "Task", "Support": "Task", "Maintenance": "Task", "Story": "Story",
}

# JPD Idea field option IDs
STRATEGIC_INITIATIVES_ID = "10574"
DISCOVERY_FIELD    = "customfield_10049"
INITIATIVE_FIELD   = "customfield_10628"
PRODUCT_CAT_FIELD  = "customfield_10391"
LABELS_FIELD       = "labels"

DISCOVERY_OPTIONS = {
    "validate": "10027", "validating": "10026", "validated": "10025",
    "won't do": "10028", "delivered": "10072",
}

INITIATIVE_OPTIONS = {
    "crm facelift": "10272", "iextend feature": "10273", "payments module": "10310",
    "insurance module": "10311", "extension feature": "10348", "compliance module": "10350",
    "ai assistant feature": "10351", "notification feature": "10384", "quoting feature": "10385",
    "onboarding module": "10386", "services module": "10387", "application module": "10388",
    "dashboard module": "10389", "training module": "10390", "complaints module": "10391",
    "claims module": "10392", "dishonours module": "10393", "task feature": "10394",
    "website": "10397", "client portal module": "10396", "client profile module": "10430",
    "system": "10463", "voa": "10576",
    # Scope tags
    "mvp": "10346", "iteration": "10347", "modules": "10533",
    "workflows": "10534", "features": "10535",
}

PRODUCT_CATEGORY_OPTIONS = {
    "analytics": "10190", "ai": "10191", "ux/ui": "10192",
    "integrations": "10193", "expansion": "10194", "feedback": "10577",
}

# Roadmap column lookup (lowercase name → option ID)
ROADMAP_COLUMN_LOOKUP = {col["value"].lower(): col["id"] for col in ROADMAP_COLUMNS}
ROADMAP_COLUMN_LOOKUP.update({"shipped": ROADMAP_SHIPPED_ID, "done": ROADMAP_DONE_ID, "backlog": ROADMAP_BACKLOG_ID})

auth    = (JIRA_EMAIL, JIRA_API_TOKEN)
headers = {"Accept": "application/json", "Content-Type": "application/json"}

def discover_reviewed_field():
    """Find the custom field ID for the 'Reviewed' text field."""
    global REVIEWED_FIELD
    try:
        r = requests.get(f"{JIRA_BASE_URL}/rest/api/3/field", auth=auth, headers=headers)
        r.raise_for_status()
        for f in r.json():
            if f.get("name") == "Reviewed" and f.get("custom", False):
                REVIEWED_FIELD = f["id"]
                log.info(f"Discovered Reviewed field: {REVIEWED_FIELD}")
                return
        log.warning("Could not find 'Reviewed' custom field — JOB 5 will skip Reviewed updates.")
    except Exception as e:
        log.error(f"Failed to discover Reviewed field: {e}")

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
    return jira_get(f"/rest/agile/1.0/sprint/{sprint_id}/issue", params={"fields": "summary,priority,status,parent", "maxResults": 200}).get("issues", [])

def get_sprint_todo_points(sprint_id):
    return sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in get_sprint_issues(sprint_id) if i["fields"]["status"]["name"] == "To Do")

def get_andrej_ready_backlog():
    jql = f'project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status = Ready AND status != Released AND assignee = "{ANDREJ_ID}" AND cf[10016] is not EMPTY'
    issues = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": "summary,priority,parent,customfield_10016", "maxResults": 200}).get("issues", [])
    issues.sort(key=lambda i: _roadmap_sort_key(i))
    return issues

def get_backlog_issues():
    return jira_get("/rest/api/3/search/jql", params={"jql": "project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status != Released AND status != Done", "fields": "summary,priority,status,parent,customfield_10020", "maxResults": 200}).get("issues", [])

def move_issue_to_sprint(issue_key, sprint_id):
    ok, _ = jira_post(f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": [issue_key]})
    return ok


def _roadmap_sort_key(issue):
    """Sort key: roadmap column rank (0=soonest) → RICE value (desc) → Jira priority.
    Tickets connected to the strategic pipeline rank before non-connected ones."""
    f = issue.get("fields", {})
    # Trace: ticket → parent Epic → EPIC_ROADMAP_RANK cache
    parent_key = (f.get("parent") or {}).get("key", "")
    epic_key = issue["key"] if (f.get("issuetype") or {}).get("name") == "Epic" else parent_key

    if epic_key and epic_key in EPIC_ROADMAP_RANK:
        col_rank, rice_value = EPIC_ROADMAP_RANK[epic_key]
        return (col_rank, -rice_value, PRIORITY_ORDER.get((f.get("priority") or {}).get("name", ""), 999))

    # Not connected to strategic pipeline — ranks after all roadmap-driven tickets
    return (500, 0, PRIORITY_ORDER.get((f.get("priority") or {}).get("name", ""), 999))


def rank_issues(issues, label):
    """Rank issues by strategic roadmap priority, falling back to Jira priority."""
    if len(issues) < 2:
        log.info(f"{label}: only {len(issues)} issue(s), no ranking needed.")
        return
    issues.sort(key=_roadmap_sort_key)
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
    jql = 'project = AX AND (Reviewed is EMPTY OR Reviewed = "Partially") AND status not in (Done, Released) ORDER BY rank ASC'
    field_list = f"summary,description,issuetype,priority,status,parent,issuelinks,attachment,assignee,{STORY_POINTS_FIELD},sprint"
    if REVIEWED_FIELD:
        field_list += f",{REVIEWED_FIELD}"
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": field_list, "maxResults": 50, "startAt": start_at})
        batch = data.get("issues", [])
        total = data.get("total", 0)
        issues.extend(batch)
        log.info(f"  Fetched {len(issues)}/{total} unreviewed issues...")
        if start_at + len(batch) >= total:
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


def update_issue_fields(issue_key, summary=None, description_md=None, story_points=None, reviewed_value="Yes"):
    """Update an issue. reviewed_value can be 'Yes', 'Partially', or None to skip."""
    payload = {"fields": {}, "update": {}}

    if summary:
        payload["fields"]["summary"] = summary
    if story_points is not None:
        payload["fields"][STORY_POINTS_FIELD] = float(story_points)
    if reviewed_value and REVIEWED_FIELD:
        payload["fields"][REVIEWED_FIELD] = reviewed_value
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
            "description": {"version": 1, "type": "doc", "content": markdown_to_adf(desc_md)},
        }
    }
    if REVIEWED_FIELD:
        payload["fields"][REVIEWED_FIELD] = "Yes"

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


def assess_completeness(issue_type, enrichment, story_points):
    """Determine if enrichment is fully complete ('Yes') or only partial ('Partially').
    Returns 'Yes' if all PM fields have real content, 'Partially' otherwise."""
    def has_value(val):
        if val is None:
            return False
        if isinstance(val, str):
            return val.strip() not in ("", "N/A", "TBD", "Not set")
        if isinstance(val, list):
            return len(val) > 0 and all(has_value(v) for v in val)
        return True

    if issue_type == "Epic":
        checks = [
            has_value(enrichment.get("pm_summary")),
            has_value(enrichment.get("validated")),
            has_value(enrichment.get("rice_score")),
            has_value(enrichment.get("prd")),
        ]
    elif issue_type == "Task":
        checks = [
            has_value(enrichment.get("pm_summary")),
            has_value(enrichment.get("user_story")),
            has_value(enrichment.get("acceptance_criteria")),
            has_value(enrichment.get("test_plan")),
            story_points is not None,
        ]
    else:  # Bug, Maintenance, Spike, Support
        checks = [
            has_value(enrichment.get("pm_summary")),
            story_points is not None,
        ]

    return "Yes" if all(checks) else "Partially"


def enrich_ticket_descriptions():
    if not ANTHROPIC_API_KEY:
        log.info("JOB 5 skipped — ANTHROPIC_API_KEY not set.")
        return

    issues = get_unreviewed_issues()
    if not issues:
        log.info("JOB 5: No unreviewed tickets found.")
        return

    log.info(f"JOB 5: Found {len(issues)} unreviewed ticket(s) to enrich.")
    type_counts = {}
    for i in issues:
        t = i["fields"]["issuetype"]["name"]
        type_counts[t] = type_counts.get(t, 0) + 1
    log.info(f"  Breakdown: {type_counts}")

    for issue in issues:
        key = issue["key"]
        f = issue["fields"]
        issue_type = f["issuetype"]["name"]
        summary = f["summary"]

        if issue_type not in SUPPORTED_TYPES:
            log.info(f"  Skipping {key} — unsupported type '{issue_type}', marking reviewed.")
            update_issue_fields(key, reviewed_value="Yes")
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
                story_points=0 if issue_type != "Epic" else None, reviewed_value="Yes")
        else:
            reviewed = assess_completeness(issue_type, enrichment, new_sp)
            update_issue_fields(key, summary=polished_summary, description_md=new_desc,
                story_points=new_sp, reviewed_value=reviewed)

        log.info(f"  Completed {key}.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 6: User Feedback Idea Processing (AR Strategic Roadmap)
# ══════════════════════════════════════════════════════════════════════════════

def get_user_feedback_ideas(scored_only=False):
    """Fetch AR ideas in the 'User Feedback' swimlane."""
    jql = f'project = {AR_PROJECT_KEY} AND cf[10694] = "User Feedback"'
    if scored_only:
        jql += f' AND cf[10526] is not EMPTY AND cf[10047] is not EMPTY AND cf[10527] is not EMPTY AND cf[10058] is not EMPTY'
    else:
        jql += f' AND (cf[10526] is EMPTY OR cf[10047] is EMPTY OR cf[10527] is EMPTY OR cf[10058] is EMPTY)'
    fields = f"summary,description,status,priority,labels,{RICE_REACH_FIELD},{RICE_IMPACT_FIELD},{RICE_CONFIDENCE_FIELD},{RICE_EFFORT_FIELD},{RICE_VALUE_FIELD},{ROADMAP_FIELD},{SWIMLANE_FIELD},{ADVISER_FIELD}"
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": fields, "maxResults": 50, "startAt": start_at})
        batch = data.get("issues", [])
        total = data.get("total", 0)
        issues.extend(batch)
        if start_at + len(batch) >= total:
            break
        start_at += len(batch)
    return issues


def build_feedback_enrichment_prompt(issue):
    """Build a prompt for Claude to clean up description and score RICE for a user feedback idea."""
    f = issue["fields"]
    summary = f["summary"]
    desc = f.get("description") or ""
    if isinstance(desc, dict):
        desc = adf_to_text(desc)

    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.
The CRM serves multiple divisions: LIP (lead intake & processing) team, services team, and advisers.

You are processing a User Feedback idea ticket from the Strategic Roadmap.

TICKET: {issue["key"]}
CURRENT SUMMARY: {summary}
RAW DESCRIPTION:
{desc}

You must do two things:

1. CLEAN UP THE DESCRIPTION:
   - The description contains verbatim user feedback quotes. Keep them verbatim — do NOT change the user's words.
   - Format each quote as: 💬 [Name]: "[exact quote text]"
   - Fix punctuation, add proper apostrophes, but don't rephrase their words.
   - If the raw text is "Name: some feedback text", convert to: 💬 Name: "some feedback text"
   - If already formatted with 💬 or 🗣, normalise to 💬 format.
   - Separate each quote with a blank line.
   - Preserve the adviser's name from each quote.

2. SCORE RICE (each 1-5):
   - **Reach** (1-5): How many users does this affect? 1=very few, 2=some, 3=moderate, 4=many, 5=nearly all users
   - **Impact** (1-5): How significant is the impact when it occurs? 1=minimal, 2=low, 3=moderate, 4=high, 5=critical/blocking
   - **Confidence** (1-5): How confident are we this is a real problem? 1=speculative, 2=low evidence, 3=some evidence, 4=strong evidence, 5=validated by multiple users
   - **Effort** (1-5): How much effort to solve? 1=trivial, 2=small, 3=medium, 4=large, 5=massive
   
   Consider: number of distinct users quoted (more = higher reach/confidence), severity of pain described,
   whether it's a bug vs feature vs workflow issue, compliance/revenue impact, and implementation complexity.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences, no extra text):
{{
  "cleaned_description": "<formatted quotes with 💬 Name: \\"quote\\" format, each separated by blank lines>",
  "adviser_names": ["<list of adviser names extracted from quotes>"],
  "rice_reach": <1-5>,
  "rice_impact": <1-5>,
  "rice_confidence": <1-5>,
  "rice_effort": <1-5>,
  "rice_reasoning": "<1-2 sentences explaining the scores>"
}}"""


def update_ar_idea(issue_key, cleaned_desc=None, rice_scores=None):
    """Update an AR idea with cleaned description and/or RICE scores."""
    payload = {"fields": {}}

    if rice_scores:
        payload["fields"][RICE_REACH_FIELD] = rice_scores.get("reach")
        payload["fields"][RICE_IMPACT_FIELD] = rice_scores.get("impact")
        payload["fields"][RICE_CONFIDENCE_FIELD] = rice_scores.get("confidence")
        payload["fields"][RICE_EFFORT_FIELD] = rice_scores.get("effort")

    if cleaned_desc:
        payload["update"] = {"description": [{"set": {"version": 1, "type": "doc", "content": markdown_to_adf(cleaned_desc)}}]}

    if not payload["fields"]:
        del payload["fields"]

    ok, r = jira_put(f"/rest/api/3/issue/{issue_key}", payload)
    if not ok:
        log.warning(f"Failed to update {issue_key}: {r.status_code} {r.text[:300]}")
    return ok


def prioritise_feedback_ideas():
    """Re-prioritise all scored User Feedback ideas across Roadmap columns by RICE score."""
    jql = f'project = {AR_PROJECT_KEY} AND cf[10694] = "User Feedback" AND cf[10526] is not EMPTY AND cf[10047] is not EMPTY AND cf[10527] is not EMPTY AND cf[10058] is not EMPTY'
    fields = f"summary,{RICE_REACH_FIELD},{RICE_IMPACT_FIELD},{RICE_CONFIDENCE_FIELD},{RICE_EFFORT_FIELD},{ROADMAP_FIELD}"
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={"jql": jql, "fields": fields, "maxResults": 100, "startAt": start_at})
        batch = data.get("issues", [])
        total = data.get("total", 0)
        issues.extend(batch)
        if start_at + len(batch) >= total:
            break
        start_at += len(batch)

    if not issues:
        log.info("  No scored User Feedback ideas to prioritise.")
        return

    # Calculate RICE value for each and sort descending
    for issue in issues:
        f = issue["fields"]
        r = f.get(RICE_REACH_FIELD) or 1
        i = f.get(RICE_IMPACT_FIELD) or 1
        c = f.get(RICE_CONFIDENCE_FIELD) or 1
        e = f.get(RICE_EFFORT_FIELD) or 1
        issue["_rice_value"] = (r * i * c) / e
    issues.sort(key=lambda x: x["_rice_value"], reverse=True)

    log.info(f"  Prioritising {len(issues)} scored User Feedback ideas across roadmap columns.")

    # Assign to columns: first column gets 2-3 tickets, rest get up to IDEAS_PER_COLUMN
    idx = 0
    for col in ROADMAP_COLUMNS:
        if idx >= len(issues):
            break
        slots = IDEAS_PER_COLUMN
        assigned = 0
        while idx < len(issues) and assigned < slots:
            issue = issues[idx]
            current_roadmap = (issue["fields"].get(ROADMAP_FIELD) or {}).get("id")
            target_id = col["id"]
            if current_roadmap != target_id:
                ok, resp = jira_put(f"/rest/api/3/issue/{issue['key']}", {
                    "fields": {ROADMAP_FIELD: {"id": target_id}}
                })
                if ok:
                    log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) → {col['value']}")
                else:
                    log.warning(f"    Failed to move {issue['key']} to {col['value']}: {resp.status_code}")
            else:
                log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) already in {col['value']}")
            idx += 1
            assigned += 1

    # Remaining ideas go to Backlog
    while idx < len(issues):
        issue = issues[idx]
        current_roadmap = (issue["fields"].get(ROADMAP_FIELD) or {}).get("id")
        if current_roadmap != ROADMAP_BACKLOG_ID:
            ok, resp = jira_put(f"/rest/api/3/issue/{issue['key']}", {
                "fields": {ROADMAP_FIELD: {"id": ROADMAP_BACKLOG_ID}}
            })
            if ok:
                log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) → Backlog")
        idx += 1


def process_user_feedback():
    """JOB 6: Process User Feedback ideas — clean descriptions, score RICE, prioritise."""
    if not ANTHROPIC_API_KEY:
        log.info("JOB 6 skipped — ANTHROPIC_API_KEY not set.")
        return

    # Step 1: Find unscored User Feedback ideas
    unscored = get_user_feedback_ideas(scored_only=False)
    if unscored:
        log.info(f"JOB 6: Found {len(unscored)} unscored User Feedback idea(s) to process.")
        for issue in unscored:
            key = issue["key"]
            summary = issue["fields"]["summary"]
            log.info(f"  Processing {key}: {summary}")

            prompt = build_feedback_enrichment_prompt(issue)
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
                continue

            cleaned_desc = enrichment.get("cleaned_description")
            rice_scores = {
                "reach": min(max(int(enrichment.get("rice_reach", 1)), 1), 5),
                "impact": min(max(int(enrichment.get("rice_impact", 1)), 1), 5),
                "confidence": min(max(int(enrichment.get("rice_confidence", 1)), 1), 5),
                "effort": min(max(int(enrichment.get("rice_effort", 1)), 1), 5),
            }
            rice_val = (rice_scores["reach"] * rice_scores["impact"] * rice_scores["confidence"]) / rice_scores["effort"]

            if update_ar_idea(key, cleaned_desc=cleaned_desc, rice_scores=rice_scores):
                log.info(f"  Completed {key}: R={rice_scores['reach']} I={rice_scores['impact']} C={rice_scores['confidence']} E={rice_scores['effort']} → Value={rice_val:.1f}")
            else:
                log.warning(f"  Failed to update {key}")
    else:
        log.info("JOB 6: No unscored User Feedback ideas found.")

    # Step 2: Re-prioritise ALL scored ideas across the roadmap
    log.info("JOB 6: Re-prioritising User Feedback ideas across roadmap columns.")
    prioritise_feedback_ideas()


# ══════════════════════════════════════════════════════════════════════════════
# JOB 7: Telegram Bot — Create JPD Ideas from Voice/Text
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(msg, parse_mode="Markdown"):
    """Send a proactive Telegram message (for briefings/alerts)."""
    chat_id = TELEGRAM_CHAT_ID
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        log.warning("Cannot send Telegram message — TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN not set.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": parse_mode, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        log.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")
    return False

def build_idea_extraction_prompt(user_text, swimlane_id=None):
    """Build the Claude prompt to structure a Telegram message into a JPD idea."""
    is_feedback = (swimlane_id == USER_FEEDBACK_OPTION_ID)
    product_cats = ", ".join(f'"{k.title()}"' for k in PRODUCT_CATEGORY_OPTIONS)

    if is_feedback:
        # Feedback ideas only need VoA initiative — no module/stage/scope
        initiative_instructions = """- INITIATIVE: For User Feedback ideas, this is always just "VoA". Do NOT include initiative_module, initiative_stage, or initiative_scope."""
        initiative_json = ""
        labels_rule = '- LABELS: Must be either "Modules" or "Features" — pick the one that best fits the idea.'
    else:
        initiative_modules = ", ".join(f'"{k.title()}"' for k in INITIATIVE_OPTIONS if k not in ("mvp", "iteration", "modules", "workflows", "features"))
        initiative_instructions = f"""- INITIATIVE must always have exactly 3 values:
  1. initiative_module: The primary module or feature. Select ONE from: {initiative_modules}
  2. initiative_stage: Either "MVP" (new capability) or "Iteration" (improving existing). Pick based on context.
  3. initiative_scope: Either "Modules" (if it's a full module/screen) or "Features" (if it's a feature within a module)."""
        initiative_json = """  "initiative_module": "[Primary module/feature — select ONE from the list below]",
  "initiative_stage": "[MVP or Iteration]",
  "initiative_scope": "[Modules or Features]","""
        labels_rule = '- LABELS: Must be either "Modules" or "Features" — match whichever you picked for initiative_scope.'

    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.

A product idea has been submitted via Telegram (possibly from a voice note transcription — it may be informal/conversational).
Your job is to extract and structure it into a fully-formed JPD idea.

USER INPUT:
{user_text}

Respond with ONLY a JSON object (no markdown, no backticks, no explanation):

{{
  "summary": "Concise idea title (3-8 words)",
  "description": "**Outcome we want to achieve**\\n\\n[Clear, specific outcome with measurable targets where possible.]\\n\\n**Why it's a problem**\\n\\n[Current pain point, inefficiency, or gap. Include evidence where available.]\\n\\n**How it gets us closer to our vision: The Adviser CRM that enables workflow and pipeline visibility, client engagement and compliance through intelligent automation.**\\n\\n[Connect to vision — workflow/pipeline visibility, client engagement, compliance, or intelligent automation.]\\n\\n**How it improves our north star: Total submissions**\\n\\n[Specific causal chain explaining how this increases total submissions.]",
  {initiative_json}
  "labels": "[Modules or Features]",
  "product_category": "[One of: {product_cats}, or null]",
  "discovery": "Validate",
  "rice_reach": [1-5 estimate],
  "rice_impact": [1-5 estimate],
  "rice_confidence": [1-5 estimate],
  "rice_effort": [1-5 estimate]
}}

RULES:
- Write the description as a thoughtful PM would — substantive, not just parroting the input.
- The four description sections are MANDATORY. Fill them all in based on the context.
{initiative_instructions}
{labels_rule}
- ROADMAP: Do NOT include a roadmap_column field. All new ideas go to Backlog automatically.
- For RICE, make your best estimate given the context. If unsure, use 3 for each.
- discovery should default to "Validate" unless the user says otherwise."""


def create_jpd_idea(structured_data, swimlane_id=None):
    """Create a JPD idea in Jira from structured data extracted by Claude."""
    if swimlane_id is None:
        swimlane_id = STRATEGIC_INITIATIVES_ID
    summary = structured_data.get("summary", "Untitled idea")
    description_md = structured_data.get("description", "")

    # Build the fields payload
    fields = {
        "project": {"key": AR_PROJECT_KEY},
        "issuetype": {"name": "Idea"},
        "summary": summary,
        "description": {"version": 1, "type": "doc", "content": markdown_to_adf(description_md)},
        "assignee": {"accountId": JAMES_ACCOUNT_ID},
        SWIMLANE_FIELD: {"id": swimlane_id},
    }

    # Roadmap — always Backlog
    fields[ROADMAP_FIELD] = {"id": ROADMAP_BACKLOG_ID}

    # Initiative — Strategic: 3 values [module, stage, scope]; Feedback: just VoA
    if swimlane_id == USER_FEEDBACK_OPTION_ID:
        voa_id = INITIATIVE_OPTIONS.get("voa")
        if voa_id:
            fields[INITIATIVE_FIELD] = [{"id": voa_id}]
    else:
        init_ids = []
        for key in ("initiative_module", "initiative_stage", "initiative_scope"):
            name = structured_data.get(key, "")
            if name:
                option_id = INITIATIVE_OPTIONS.get(name.lower())
                if option_id:
                    init_ids.append({"id": option_id})
        if init_ids:
            fields[INITIATIVE_FIELD] = init_ids

    # Labels — only "Modules" or "Features"
    label = structured_data.get("labels", "Features")
    if isinstance(label, list):
        label = label[0] if label else "Features"
    if label not in ("Modules", "Features"):
        label = "Features"
    fields[LABELS_FIELD] = [label]

    # Product category
    prod_cat = structured_data.get("product_category")
    if prod_cat and prod_cat.lower() in PRODUCT_CATEGORY_OPTIONS:
        fields[PRODUCT_CAT_FIELD] = [{"id": PRODUCT_CATEGORY_OPTIONS[prod_cat.lower()]}]

    # Discovery status
    discovery = structured_data.get("discovery", "Validate")
    if discovery and discovery.lower() in DISCOVERY_OPTIONS:
        fields[DISCOVERY_FIELD] = {"id": DISCOVERY_OPTIONS[discovery.lower()]}

    # RICE scores
    for key, field_id in [("rice_reach", RICE_REACH_FIELD), ("rice_impact", RICE_IMPACT_FIELD),
                          ("rice_confidence", RICE_CONFIDENCE_FIELD), ("rice_effort", RICE_EFFORT_FIELD)]:
        val = structured_data.get(key)
        if val is not None:
            fields[field_id] = min(max(int(val), 1), 5)

    ok, resp = jira_post("/rest/api/3/issue", {"fields": fields})
    if ok:
        issue_key = resp.json().get("key", "?")
        log.info(f"  JOB 7: Created JPD idea {issue_key}: {summary}")
        return issue_key
    else:
        log.error(f"  JOB 7: Failed to create idea: {resp.status_code} {resp.text[:300]}")
        return None


def transcribe_voice(file_path):
    """Transcribe a voice note using SpeechRecognition + Google free API."""
    try:
        import speech_recognition as sr
        from pydub import AudioSegment

        # Convert OGG to WAV
        wav_path = file_path.replace(".ogg", ".wav")
        audio = AudioSegment.from_ogg(file_path)
        audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        text = recognizer.recognize_google(audio_data)
        log.info(f"  JOB 7: Transcribed voice: {text[:100]}...")
        return text
    except sr.UnknownValueError:
        log.warning("  JOB 7: Could not understand the voice note.")
        return None
    except sr.RequestError as e:
        log.error(f"  JOB 7: Speech recognition service error: {e}")
        return None
    except Exception as e:
        log.error(f"  JOB 7: Transcription error: {e}")
        return None
    finally:
        # Clean up temp files
        for p in [file_path, file_path.replace(".ogg", ".wav")]:
            try:
                os.remove(p)
            except OSError:
                pass


def process_telegram_idea(user_text, chat_id, bot, swimlane_id=None):
    """Full pipeline: text → Claude structuring → Jira creation → Telegram reply."""
    if swimlane_id is None:
        swimlane_id = STRATEGIC_INITIATIVES_ID
    if not user_text or not user_text.strip():
        bot.send_message(chat_id, "❌ Couldn't understand the message. Please try again.")
        return

    bot.send_message(chat_id, "🧠 Structuring your idea...")

    # Call Claude to structure the idea
    prompt = build_idea_extraction_prompt(user_text, swimlane_id=swimlane_id)
    response = call_claude(prompt, max_tokens=2048)
    if not response:
        bot.send_message(chat_id, "❌ Failed to process with AI. Check the Anthropic API key.")
        return

    # Parse Claude's JSON response
    try:
        clean = re.sub(r'^```(?:json)?\s*', '', response)
        clean = re.sub(r'\s*```$', '', clean)
        structured = json.loads(clean)
    except json.JSONDecodeError as e:
        log.error(f"  JOB 7: JSON parse error: {e}\nRaw response: {response[:500]}")
        bot.send_message(chat_id, "❌ Failed to parse AI response. Please try again.")
        return

    # Create the Jira idea
    issue_key = create_jpd_idea(structured, swimlane_id=swimlane_id)
    if issue_key:
        summary = structured.get("summary", "Untitled")
        rice_r = structured.get("rice_reach", 3)
        rice_i = structured.get("rice_impact", 3)
        rice_c = structured.get("rice_confidence", 3)
        rice_e = structured.get("rice_effort", 3)
        try:
            rice_value = (int(rice_r) * int(rice_i) * int(rice_c)) / int(rice_e)
        except (ValueError, ZeroDivisionError):
            rice_value = 0

        link = f"https://axiscrm.atlassian.net/jira/polaris/projects/AR/ideas/view/11184018?selectedIssue={issue_key}"
        lane_name = "User Feedback" if swimlane_id == USER_FEEDBACK_OPTION_ID else "Strategic Initiatives"

        if swimlane_id == USER_FEEDBACK_OPTION_ID:
            initiative_line = "🏷 VoA"
        else:
            init_module = structured.get("initiative_module", "?")
            init_stage = structured.get("initiative_stage", "?")
            init_scope = structured.get("initiative_scope", "?")
            initiative_line = f"🏷 {init_module} · {init_stage} · {init_scope}"

        msg = (
            f"✅ *{issue_key}* — {summary}\n\n"
            f"🏊 {lane_name}\n"
            f"{initiative_line}\n"
            f"📊 Value: {rice_value:.1f} / 125\n\n"
            f"[Open on board]({link})"
        )
        bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, "❌ Failed to create the idea in Jira. Check the logs.")


def start_telegram_bot():
    """Start the Telegram bot with polling in the current thread."""
    if not TELEGRAM_BOT_TOKEN:
        log.info("JOB 7: Telegram bot skipped — TELEGRAM_BOT_TOKEN not set.")
        return

    try:
        import telebot
    except ImportError:
        log.error("JOB 7: pyTelegramBotAPI not installed. Add 'pyTelegramBotAPI' to requirements.txt.")
        return

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    user_mode = {}

    def save_chat_id(chat_id):
        """Auto-capture chat ID for proactive messaging."""
        global TELEGRAM_CHAT_ID
        if not TELEGRAM_CHAT_ID:
            TELEGRAM_CHAT_ID = str(chat_id)
            log.info(f"Telegram chat ID captured: {TELEGRAM_CHAT_ID}")

    @bot.message_handler(commands=["start", "help"])
    def handle_start(message):
        save_chat_id(message.chat.id)
        bot.reply_to(message,
            "👋 *Alfred — Axis CRM Bot*\n\n"
            "*🧠 /strategic* — Create idea → Strategic Initiatives swimlane\n"
            "*💬 /feedback* — Create idea → User Feedback swimlane\n"
            "*🔨 /backlog* — Create Epic + broken-down tickets in Sprints\n"
            "*✏️ /update* — Edit an existing ticket (summary, description, fields)\n"
            "*➕ /add* — Create tickets under an existing epic\n"
            "*📋 /productweekly* — Review actions & add callouts to weekly meeting\n\n"
            "Send a text or voice note after selecting a mode.\n"
            "Default mode: /strategic",
            parse_mode="Markdown"
        )

    @bot.message_handler(commands=["strategic"])
    def handle_strategic(message):
        save_chat_id(message.chat.id)
        user_mode[message.chat.id] = {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID}
        bot.reply_to(message, "🧠 *Strategic Initiatives* — send your idea.", parse_mode="Markdown")

    @bot.message_handler(commands=["feedback"])
    def handle_feedback(message):
        save_chat_id(message.chat.id)
        user_mode[message.chat.id] = {"mode": "roadmap", "swimlane": USER_FEEDBACK_OPTION_ID}
        bot.reply_to(message, "💬 *User Feedback* — send your idea.", parse_mode="Markdown")

    @bot.message_handler(commands=["backlog"])
    def handle_backlog_mode(message):
        save_chat_id(message.chat.id)
        user_mode[message.chat.id] = {"mode": "backlog", "swimlane": STRATEGIC_INITIATIVES_ID}
        bot.reply_to(message, "🔨 *Backlog mode* — describe what needs building.", parse_mode="Markdown")

    @bot.message_handler(commands=["update"])
    def handle_update(message):
        save_chat_id(message.chat.id)
        user_mode[message.chat.id] = {"mode": "update"}
        bot.reply_to(message,
            "✏️ *Update mode* — send the ticket ID and what to change.\n\n"
            "Examples:\n"
            "• `AX-123 change acceptance criteria to include admin validation`\n"
            "• `AX-456 set story points to 2`\n"
            "• `AR-78 update summary to Campaign ROI Dashboard`\n\n"
            "Or just send the ticket ID first, then the changes.",
            parse_mode="Markdown")

    @bot.message_handler(commands=["add"])
    def handle_add(message):
        save_chat_id(message.chat.id)
        user_mode[message.chat.id] = {"mode": "add"}
        bot.reply_to(message,
            "➕ *Add mode* — create tickets under an existing epic.\n\n"
            "Examples:\n"
            "• `AX-50 bug: date picker crashes when selecting past dates`\n"
            "• `AX-50 task: add CSV export to campaign report`\n"
            "• `AX-50 need a spike to investigate API rate limits and a task to add retry logic`\n\n"
            "Or just send the epic ID first, then describe the ticket(s).",
            parse_mode="Markdown")

    @bot.message_handler(commands=["productweekly"])
    def handle_product_weekly(message):
        save_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "📋 Loading Product Weekly actions...", parse_mode="Markdown")

        # Check if THIS week's page exists (i.e. for the upcoming/current Friday)
        sydney_tz = pytz.timezone("Australia/Sydney")
        now = datetime.now(sydney_tz)
        # Find next Friday (or today if it's Friday)
        days_until_friday = (4 - now.weekday()) % 7
        if days_until_friday == 0 and now.hour < 7:
            # It's Friday but before 7am — page hasn't been created yet
            friday_date = now.date()
        elif days_until_friday == 0:
            friday_date = now.date()
        else:
            friday_date = (now + timedelta(days=days_until_friday)).date()

        expected_title = f"{friday_date.strftime('%Y-%m-%d')} Product Weekly"

        # Check if this week's page exists
        existing = confluence_get("/rest/api/search", params={
            "cql": f'ancestor = {WEEKLY_PARENT_PAGE_ID} AND type = page AND title = "{expected_title}"',
            "limit": 1,
        })
        this_week_exists = existing and existing.get("results")

        if this_week_exists:
            # Page exists — normal flow
            page_id = existing["results"][0]["content"]["id"]
            title = existing["results"][0]["title"]
        else:
            # Page doesn't exist yet — use last week's for action review, buffer callouts
            page_id, title = get_current_weekly_page()

        if not page_id:
            bot.send_message(message.chat.id, "❌ No Product Weekly page found.")
            return

        adf = get_page_adf(page_id)
        if not adf:
            bot.send_message(message.chat.id, "❌ Failed to load the page.")
            return

        items = extract_action_items_from_adf(adf)

        msg = f"📋 *{title}*\n\n"

        if not this_week_exists:
            msg += f"⏳ _This week's page ({expected_title}) will be created at 7am Friday._\n"
            msg += f"_Callouts you send now will be added to the new page automatically._\n\n"

        if items:
            msg += "*Actions from last meeting:*\n"
            for i, item in enumerate(items):
                icon = "✅" if item["state"] == "DONE" else "⬜"
                msg += f"{i+1}. {icon} {item['person']}: {item['text']}\n"
            msg += "\n*Reply with:*\n"
            msg += "• Numbers to mark done (e.g. `1 3`)\n"
            msg += "• Text to add a callout\n"
            msg += "• `/done` when finished"
        else:
            msg += "No action items found.\n\n*Send text to add a callout.*"

        user_mode[message.chat.id] = {
            "mode": "weekly",
            "page_id": page_id,
            "title": title,
            "items": items,
            "this_week_exists": bool(this_week_exists),
        }
        bot.send_message(message.chat.id, msg, parse_mode="Markdown")

    @bot.message_handler(commands=["done"])
    def handle_done(message):
        save_chat_id(message.chat.id)
        state = user_mode.get(message.chat.id, {})
        mode = state.get("mode")
        if mode == "weekly":
            user_mode[message.chat.id] = {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID}
            bot.reply_to(message, "✅ Product Weekly session finished.", parse_mode="Markdown")
        elif mode == "update":
            user_mode[message.chat.id] = {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID}
            bot.reply_to(message, "✅ Update mode finished.", parse_mode="Markdown")
        elif mode == "add":
            user_mode[message.chat.id] = {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID}
            bot.reply_to(message, "✅ Add mode finished.", parse_mode="Markdown")
        else:
            bot.reply_to(message, "Nothing to finish. Use /help for commands.")

    @bot.message_handler(content_types=["voice"])
    def handle_voice(message):
        save_chat_id(message.chat.id)
        try:
            bot.send_message(message.chat.id, "🎙 Transcribing your voice note...")
            file_info = bot.get_file(message.voice.file_id)
            downloaded = bot.download_file(file_info.file_path)
            tmp_path = f"/tmp/voice_{message.message_id}.ogg"
            with open(tmp_path, "wb") as f:
                f.write(downloaded)

            text = transcribe_voice(tmp_path)
            if text:
                bot.send_message(message.chat.id, f"📝 Heard: _{text}_", parse_mode="Markdown")
                state = user_mode.get(message.chat.id, {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID})
                if state.get("mode") == "weekly":
                    # Treat voice as a callout in weekly mode
                    page_id = state.get("page_id")
                    this_week_exists = state.get("this_week_exists", True)
                    if this_week_exists:
                        polished = add_callout_to_weekly(page_id, text)
                        if polished:
                            bot.send_message(message.chat.id,
                                f"📢 Callout added to the page:\n_{polished}_\n\nSend more or /done to finish.",
                                parse_mode="Markdown")
                        else:
                            bot.send_message(message.chat.id, "❌ Failed to add callout to the page.")
                    else:
                        polished = polish_callout(text)
                        pending_weekly_callouts.append(polished)
                        bot.send_message(message.chat.id,
                            f"📢 Callout buffered for Friday's page:\n_{polished}_\n"
                            f"({len(pending_weekly_callouts)} callout(s) queued)\n\n"
                            f"Send more or /done to finish.",
                            parse_mode="Markdown")
                elif state.get("mode") == "update":
                    process_telegram_update(text, message.chat.id, bot, state, user_mode)
                elif state.get("mode") == "add":
                    process_telegram_add(text, message.chat.id, bot, state, user_mode)
                elif state["mode"] == "backlog":
                    process_telegram_work(text, message.chat.id, bot)
                else:
                    process_telegram_idea(text, message.chat.id, bot, swimlane_id=state["swimlane"])
            else:
                bot.send_message(message.chat.id, "❌ Couldn't transcribe the voice note. Try sending it as text instead.")
        except Exception as e:
            log.error(f"Telegram voice handling error: {e}")
            bot.send_message(message.chat.id, f"❌ Error processing voice note: {e}")

    @bot.message_handler(content_types=["text"])
    def handle_text(message):
        save_chat_id(message.chat.id)
        if message.text.startswith("/"):
            bot.reply_to(message, "Unknown command. Try /strategic, /feedback, /backlog, /update, /add, /productweekly, or /help")
            return
        state = user_mode.get(message.chat.id, {"mode": "roadmap", "swimlane": STRATEGIC_INITIATIVES_ID})

        if state.get("mode") == "weekly":
            page_id = state.get("page_id")
            items = state.get("items", [])
            this_week_exists = state.get("this_week_exists", True)
            text = message.text.strip()

            # Check if it's numbers (marking actions complete)
            parts = text.replace(",", " ").split()
            if all(p.isdigit() for p in parts) and parts:
                completed = []
                for p in parts:
                    idx = int(p) - 1  # 1-indexed to 0-indexed
                    if 0 <= idx < len(items) and items[idx]["state"] == "TODO":
                        if tick_action_item(page_id, idx):
                            items[idx]["state"] = "DONE"
                            completed.append(items[idx]["text"])
                if completed:
                    bot.send_message(message.chat.id,
                        f"✅ Marked as done:\n" + "\n".join(f"• {c}" for c in completed) +
                        "\n\nSend more numbers, text for callouts, or /done to finish.",
                        parse_mode="Markdown")
                else:
                    bot.send_message(message.chat.id, "Those items were already done or invalid.")
            else:
                # It's a callout
                if this_week_exists:
                    # Page exists — add directly (polished inside add_callout_to_weekly)
                    polished = add_callout_to_weekly(page_id, text)
                    if polished:
                        bot.send_message(message.chat.id,
                            f"📢 Callout added to the page:\n_{polished}_\n\nSend more or /done to finish.",
                            parse_mode="Markdown")
                    else:
                        bot.send_message(message.chat.id, "❌ Failed to add callout to the page.")
                else:
                    # Page not created yet — polish and buffer for Friday 7am
                    polished = polish_callout(text)
                    pending_weekly_callouts.append(polished)
                    bot.send_message(message.chat.id,
                        f"📢 Callout buffered for Friday's page:\n_{polished}_\n"
                        f"({len(pending_weekly_callouts)} callout(s) queued)\n\n"
                        f"Send more or /done to finish.",
                        parse_mode="Markdown")
        elif state.get("mode") == "update":
            process_telegram_update(message.text.strip(), message.chat.id, bot, state, user_mode)
        elif state.get("mode") == "add":
            process_telegram_add(message.text.strip(), message.chat.id, bot, state, user_mode)
        elif state["mode"] == "backlog":
            process_telegram_work(message.text, message.chat.id, bot)
        else:
            process_telegram_idea(message.text, message.chat.id, bot, swimlane_id=state["swimlane"])

    log.info("JOB 7: Telegram bot starting (polling)...")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20)
    except Exception as e:
        log.error(f"JOB 7: Telegram bot crashed: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# JOB 8: Telegram Bot — Create AX Epic + Child Tickets from Voice/Text
# ══════════════════════════════════════════════════════════════════════════════

READY_TRANSITION_ID = "7"

AX_DESCRIPTION_TEMPLATES = {
    "Task": """**Product Manager:**
1. **Summary:** {summary}
2. **User story:** {user_story}
3. **Acceptance criteria:**
{acceptance_criteria}
4. **Test plan:**
{test_plan}

**Engineer:**
1. **Technical plan:**
2. **Story points estimated:**
3. **Task broken down (<=3 story points or split into parts):**

{dor_dod}""",

    "Epic": """**Product Manager:**
1. **Summary:** {summary}
2. **Validated:** No
3. **RICE score:**
4. **PRD:**

{dor_dod}""",

    "Maintenance": """**Product Manager:**
1. **Summary:** {summary}

**Engineer:**
1. **Task:**

{dor_dod}""",

    "Bug": """**Product Manager:**
1. **Summary:** {summary}

**Engineer:**
1. **Investigation:**

{dor_dod}""",
}
AX_DESCRIPTION_TEMPLATES["Support"] = AX_DESCRIPTION_TEMPLATES["Bug"]
AX_DESCRIPTION_TEMPLATES["Spike"] = AX_DESCRIPTION_TEMPLATES["Bug"]


def build_work_breakdown_prompt(user_text):
    """Build the Claude prompt to structure a Telegram message into an AX Epic + child tickets."""
    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.
The CRM serves multiple divisions: LIP (lead intake & processing) team, services team, and advisers.

A product requirement has been submitted via Telegram (possibly from a voice note transcription — it may be informal).
Your job is to:
1. Create an Epic that captures the overall initiative
2. Break it down into individual shippable tickets (Task, Bug, Spike, Support, Maintenance)
3. Each ticket must be <=3 story points (3pts = 6hrs, 2pts = 4hrs, 1pt = 2hrs, 0.5pt = 1hr, 0.25pt = 30min)

USER INPUT:
{user_text}

Respond with ONLY a JSON object (no markdown, no backticks, no explanation):

{{
  "epic": {{
    "summary": "Concise epic title",
    "description_summary": "One paragraph describing the epic scope and goals",
    "priority": "Medium"
  }},
  "tickets": [
    {{
      "type": "Task",
      "summary": "Clear task summary as a user story where appropriate",
      "priority": "Medium",
      "story_points": 2,
      "user_story": "As a [user], I want [goal] so that [benefit]",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "test_plan": ["Test step 1", "Test step 2"]
    }},
    {{
      "type": "Spike",
      "summary": "Investigation: [what needs investigating]",
      "priority": "High",
      "story_points": 1,
      "investigation_summary": "Brief description of what to investigate"
    }}
  ]
}}

RULES:
- ALWAYS create at least 2 tickets under the epic.
- Ticket types: Task, Bug, Spike, Support, Maintenance. Choose the most appropriate for each piece of work.
- Tasks MUST include user_story, acceptance_criteria (array), and test_plan (array).
- Bug, Spike, Support only need summary and investigation_summary.
- Maintenance only needs summary.
- NO ticket can exceed 3 story points. If work is large, split into multiple tickets.
- story_points must be one of: 0.25, 0.5, 1, 2, 3.
- Priority: choose from Lowest, Low, Medium, High, Highest based on urgency/impact.
- Epic priority should reflect the overall importance.
- Write substantive descriptions — don't just parrot the input.
- Think about what a developer actually needs to build this."""


def create_ax_ticket(ticket_data, issue_type, parent_key=None):
    """Create a single AX ticket with the correct description template."""
    summary = ticket_data.get("summary", "Untitled")
    priority = ticket_data.get("priority", "Medium")
    story_points = ticket_data.get("story_points")

    # Build description from template
    if issue_type == "Task":
        ac_items = ticket_data.get("acceptance_criteria", [])
        ac_str = "\n".join(f"   - {item}" for item in ac_items) if ac_items else "   - TBD"
        tp_items = ticket_data.get("test_plan", [])
        tp_str = "\n".join(f"   - {item}" for item in tp_items) if tp_items else "   - TBD"
        desc_md = AX_DESCRIPTION_TEMPLATES["Task"].format(
            summary=summary,
            user_story=ticket_data.get("user_story", ""),
            acceptance_criteria=ac_str,
            test_plan=tp_str,
            dor_dod=DOR_DOD_TASK,
        )
    elif issue_type == "Epic":
        desc_md = AX_DESCRIPTION_TEMPLATES["Epic"].format(
            summary=ticket_data.get("description_summary", summary),
            dor_dod=DOR_DOD_EPIC,
        )
    elif issue_type == "Maintenance":
        desc_md = AX_DESCRIPTION_TEMPLATES["Maintenance"].format(
            summary=summary,
            dor_dod=DOR_DOD_TASK,
        )
    else:  # Bug, Spike, Support
        inv = ticket_data.get("investigation_summary", summary)
        desc_md = AX_DESCRIPTION_TEMPLATES.get(issue_type, AX_DESCRIPTION_TEMPLATES["Bug"]).format(
            summary=inv,
            dor_dod=DOR_DOD_TASK,
        )

    fields = {
        "project": {"key": "AX"},
        "issuetype": {"name": issue_type},
        "summary": summary,
        "description": {"version": 1, "type": "doc", "content": markdown_to_adf(desc_md)},
        "assignee": {"accountId": ANDREJ_ID},
        "priority": {"name": priority},
    }

    # Parent (for child tickets under Epic)
    if parent_key and issue_type != "Epic":
        fields["parent"] = {"key": parent_key}

    # Story points (not for Epics)
    if story_points and issue_type != "Epic":
        fields[STORY_POINTS_FIELD] = float(story_points)

    ok, resp = jira_post("/rest/api/3/issue", {"fields": fields})
    if ok:
        issue_key = resp.json().get("key", "?")
        log.info(f"  JOB 8: Created {issue_type} {issue_key}: {summary}")
        return issue_key
    else:
        log.error(f"  JOB 8: Failed to create {issue_type}: {resp.status_code} {resp.text[:300]}")
        return None


def transition_to_ready(issue_key):
    """Transition an AX ticket to Ready status."""
    ok, resp = jira_post(f"/rest/api/3/issue/{issue_key}/transitions", {
        "transition": {"id": READY_TRANSITION_ID}
    })
    if ok:
        log.info(f"  JOB 8: Transitioned {issue_key} → Ready")
    else:
        log.warning(f"  JOB 8: Failed to transition {issue_key}: {resp.status_code} {resp.text[:200]}")
    return ok


def extract_ticket_key(text):
    """Extract a Jira ticket key (AX-123 or AR-45) from the start of text. Returns (key, remaining_text) or (None, text)."""
    m = re.match(r'\s*((?:AX|AR|ARU)-\d+)\s*(.*)', text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None, text


def build_update_prompt(issue_key, issue_type, current_summary, current_desc_text, current_sp, user_instruction):
    """Build Claude prompt to interpret update instructions."""
    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.

You need to apply an update to an existing Jira ticket based on the user's instruction.

CURRENT TICKET:
- Key: {issue_key}
- Type: {issue_type}
- Summary: {current_summary}
- Story Points: {current_sp}
- Current Description:
{current_desc_text}

USER INSTRUCTION:
{user_instruction}

Respond with ONLY a JSON object (no markdown, no backticks):

{{
  "summary": "Updated summary (or null to keep current)",
  "story_points": null,
  "description_changes": "A clear description of what sections to update and the new content. Be specific about which PM/Engineer sections to modify. Set to null if no description changes.",
  "updated_description": "The FULL updated description in markdown if changes are needed (preserving the existing template structure and all sections). Set to null if no description changes."
}}

RULES:
- Only change what the user asked for. Preserve everything else exactly.
- If the user mentions story points or SP, update story_points (must be 0.25, 0.5, 1, 2, or 3).
- If the user mentions summary/title, update summary.
- For description changes, always preserve the PM/Engineer section structure and DoR/DoD links.
- Be thoughtful — a brief instruction like "add admin validation to AC" means add it to existing acceptance criteria, not replace them.
- Set fields to null if they shouldn't change."""


def process_telegram_update(text, chat_id, bot, state, user_mode):
    """Process an update instruction for an existing ticket."""
    ticket_key = state.get("ticket_key")
    instruction = text

    # If no ticket key yet, try to extract from this message
    if not ticket_key:
        ticket_key, instruction = extract_ticket_key(text)
        if not ticket_key:
            bot.send_message(chat_id, "❓ I need a ticket ID (e.g. AX-123). Send the ticket ID and what to change.")
            return
        state["ticket_key"] = ticket_key
        user_mode[chat_id] = state

    # If we have a key but no instruction, ask for it
    if not instruction:
        # Fetch and show current ticket
        bot.send_message(chat_id, f"🔍 Loading {ticket_key}...")
        issue = jira_get(f"/rest/api/3/issue/{ticket_key}", params={
            "fields": f"summary,issuetype,status,{STORY_POINTS_FIELD},description"
        })
        if not issue or "fields" not in issue:
            bot.send_message(chat_id, f"❌ Couldn't find {ticket_key}. Check the ticket ID.")
            state.pop("ticket_key", None)
            return

        f = issue["fields"]
        summary = f.get("summary", "")
        itype = f.get("issuetype", {}).get("name", "?")
        status = f.get("status", {}).get("name", "?")
        sp = f.get(STORY_POINTS_FIELD) or "—"

        bot.send_message(chat_id,
            f"✏️ *{ticket_key}* ({itype} · {status} · {sp} SP)\n"
            f"_{summary}_\n\n"
            f"What do you want to change?",
            parse_mode="Markdown")
        return

    # We have both key and instruction — process
    bot.send_message(chat_id, f"✏️ Updating {ticket_key}...")

    issue = jira_get(f"/rest/api/3/issue/{ticket_key}", params={
        "fields": f"summary,issuetype,status,{STORY_POINTS_FIELD},description"
    })
    if not issue or "fields" not in issue:
        bot.send_message(chat_id, f"❌ Couldn't find {ticket_key}.")
        return

    f = issue["fields"]
    current_summary = f.get("summary", "")
    itype = f.get("issuetype", {}).get("name", "Task")
    current_sp = f.get(STORY_POINTS_FIELD)
    desc_adf = f.get("description") or {}
    current_desc_text = adf_to_text(desc_adf) if desc_adf else ""

    prompt = build_update_prompt(ticket_key, itype, current_summary, current_desc_text, current_sp, instruction)
    response = call_claude(prompt, max_tokens=4096)
    if not response:
        bot.send_message(chat_id, "❌ AI processing failed.")
        return

    try:
        clean = re.sub(r'^```(?:json)?\s*', '', response)
        clean = re.sub(r'\s*```$', '', clean)
        updates = json.loads(clean)
    except json.JSONDecodeError as e:
        log.error(f"Update parse error: {e}\nRaw: {response[:500]}")
        bot.send_message(chat_id, "❌ Failed to parse AI response. Try rephrasing.")
        return

    # Apply updates
    new_summary = updates.get("summary")
    new_sp = updates.get("story_points")
    new_desc = updates.get("updated_description")

    changes = []
    if new_summary and new_summary != current_summary:
        changes.append(f"📝 Summary → _{new_summary}_")
    if new_sp is not None and new_sp != current_sp:
        changes.append(f"🎯 Story Points → {new_sp}")
    if new_desc:
        desc_change = updates.get("description_changes", "Description updated")
        changes.append(f"📄 {desc_change}")

    if not changes:
        bot.send_message(chat_id, f"🤷 No changes needed for {ticket_key} based on your instruction.")
        return

    ok = update_issue_fields(
        ticket_key,
        summary=new_summary if new_summary and new_summary != current_summary else None,
        description_md=new_desc,
        story_points=new_sp if new_sp is not None else None,
        reviewed_value=None,  # Don't change reviewed status
    )

    if ok:
        link = f"https://axiscrm.atlassian.net/browse/{ticket_key}"
        change_list = "\n".join(changes)
        bot.send_message(chat_id,
            f"✅ *{ticket_key} updated:*\n{change_list}\n\n"
            f"[Open ticket]({link})\n\n"
            f"Send another ticket ID + changes, or /done to exit.",
            parse_mode="Markdown", disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, f"❌ Failed to update {ticket_key}. Check the logs.")

    # Clear ticket key for next update
    state.pop("ticket_key", None)
    user_mode[chat_id] = state


def build_add_prompt(epic_key, epic_summary, user_instruction):
    """Build Claude prompt to create child tickets under an epic."""
    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.

You need to create one or more child tickets under an existing epic based on the user's description.

PARENT EPIC:
- Key: {epic_key}
- Summary: {epic_summary}

USER REQUEST:
{user_instruction}

Respond with ONLY a JSON object (no markdown, no backticks):

{{
  "tickets": [
    {{
      "type": "Task",
      "summary": "Clear task summary",
      "priority": "Medium",
      "story_points": 2,
      "user_story": "As a [user], I want [goal] so that [benefit]",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "test_plan": ["Test step 1", "Test step 2"]
    }}
  ]
}}

RULES:
- Ticket types: Task, Bug, Spike, Support, Maintenance. Choose the most appropriate.
- Tasks MUST include user_story, acceptance_criteria (array), and test_plan (array).
- Bug, Spike, Support only need summary and investigation_summary.
- Maintenance only needs summary.
- NO ticket can exceed 3 story points (3pts = 6hrs, 2pts = 4hrs, 1pt = 2hrs, 0.5pt = 1hr, 0.25pt = 30min).
- If the user describes one ticket, create one. If they describe multiple, create multiple.
- story_points must be one of: 0.25, 0.5, 1, 2, 3.
- Priority: choose from Lowest, Low, Medium, High, Highest.
- Write substantive descriptions — don't just parrot the input."""


def process_telegram_add(text, chat_id, bot, state, user_mode):
    """Process adding new ticket(s) under an existing epic."""
    epic_key = state.get("epic_key")
    instruction = text

    # If no epic key yet, try to extract from this message
    if not epic_key:
        epic_key, instruction = extract_ticket_key(text)
        if not epic_key:
            bot.send_message(chat_id, "❓ I need an epic ID (e.g. AX-50). Send the epic ID and describe the ticket(s).")
            return
        state["epic_key"] = epic_key
        user_mode[chat_id] = state

    # Validate it's an Epic
    if not state.get("epic_validated"):
        issue = jira_get(f"/rest/api/3/issue/{epic_key}", params={"fields": "summary,issuetype"})
        if not issue or "fields" not in issue:
            bot.send_message(chat_id, f"❌ Couldn't find {epic_key}. Check the ticket ID.")
            state.pop("epic_key", None)
            return

        itype = issue["fields"].get("issuetype", {}).get("name", "")
        if itype != "Epic":
            bot.send_message(chat_id, f"⚠️ {epic_key} is a {itype}, not an Epic. Send an Epic ID.")
            state.pop("epic_key", None)
            return

        state["epic_summary"] = issue["fields"].get("summary", "")
        state["epic_validated"] = True
        user_mode[chat_id] = state

    # If no instruction, ask for it
    if not instruction:
        bot.send_message(chat_id,
            f"➕ *{epic_key}* — _{state.get('epic_summary', '')}_\n\n"
            f"Describe the ticket(s) to add (type, what it does, etc.).",
            parse_mode="Markdown")
        return

    # We have both — process
    bot.send_message(chat_id, f"➕ Creating ticket(s) under {epic_key}...")

    prompt = build_add_prompt(epic_key, state.get("epic_summary", ""), instruction)
    response = call_claude(prompt, max_tokens=4096)
    if not response:
        bot.send_message(chat_id, "❌ AI processing failed.")
        return

    try:
        clean = re.sub(r'^```(?:json)?\s*', '', response)
        clean = re.sub(r'\s*```$', '', clean)
        structured = json.loads(clean)
    except json.JSONDecodeError as e:
        log.error(f"Add parse error: {e}\nRaw: {response[:500]}")
        bot.send_message(chat_id, "❌ Failed to parse AI response. Try rephrasing.")
        return

    tickets = structured.get("tickets", [])
    if not tickets:
        bot.send_message(chat_id, "❌ No tickets generated. Try describing the work differently.")
        return

    created = []
    total_pts = 0
    for ticket in tickets:
        ticket_type = ticket.get("type", "Task")
        if ticket_type not in ("Task", "Bug", "Spike", "Support", "Maintenance"):
            ticket_type = "Task"
        child_key = create_ax_ticket(ticket, ticket_type, parent_key=epic_key)
        if child_key:
            transition_to_ready(child_key)
            pts = ticket.get("story_points", 0) or 0
            total_pts += pts
            created.append({"key": child_key, "type": ticket_type, "summary": ticket.get("summary", ""), "points": pts})

    if created:
        ticket_lines = "\n".join(
            f"  • {t['key']} ({t['type']}, {t['points']}SP) — {t['summary'][:60]}"
            for t in created
        )
        epic_link = f"https://axiscrm.atlassian.net/browse/{epic_key}"
        bot.send_message(chat_id,
            f"✅ *{len(created)} ticket(s)* added to {epic_key} ({total_pts} SP):\n"
            f"{ticket_lines}\n\n"
            f"[Open epic]({epic_link})\n\n"
            f"Send more tickets to add, a different epic ID, or /done to exit.",
            parse_mode="Markdown", disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, "❌ Failed to create tickets. Check the logs.")


def process_telegram_work(user_text, chat_id, bot):
    """Full pipeline: text → Claude breakdown → Epic + child tickets → Telegram reply."""
    if not user_text or not user_text.strip():
        bot.send_message(chat_id, "❌ Couldn't understand the message. Please try again.")
        return

    bot.send_message(chat_id, "🔨 Breaking down your work into tickets...")

    # Call Claude to structure the work
    prompt = build_work_breakdown_prompt(user_text)
    response = call_claude(prompt, max_tokens=4096)
    if not response:
        bot.send_message(chat_id, "❌ Failed to process with AI. Check the Anthropic API key.")
        return

    # Parse Claude's JSON response
    try:
        clean = re.sub(r'^```(?:json)?\s*', '', response)
        clean = re.sub(r'\s*```$', '', clean)
        structured = json.loads(clean)
    except json.JSONDecodeError as e:
        log.error(f"  JOB 8: JSON parse error: {e}\nRaw response: {response[:500]}")
        bot.send_message(chat_id, "❌ Failed to parse AI response. Please try again.")
        return

    # Step 1: Create the Epic
    epic_data = structured.get("epic", {})
    epic_key = create_ax_ticket(epic_data, "Epic")
    if not epic_key:
        bot.send_message(chat_id, "❌ Failed to create the Epic in Jira. Check the logs.")
        return
    transition_to_ready(epic_key)

    # Step 2: Create child tickets
    tickets = structured.get("tickets", [])
    created_tickets = []
    total_points = 0
    for ticket in tickets:
        ticket_type = ticket.get("type", "Task")
        if ticket_type not in ("Task", "Bug", "Spike", "Support", "Maintenance"):
            ticket_type = "Task"
        child_key = create_ax_ticket(ticket, ticket_type, parent_key=epic_key)
        if child_key:
            transition_to_ready(child_key)
            pts = ticket.get("story_points", 0) or 0
            total_points += pts
            created_tickets.append({
                "key": child_key,
                "type": ticket_type,
                "summary": ticket.get("summary", ""),
                "points": pts,
            })

    # Step 3: Send summary to Telegram
    epic_link = f"https://axiscrm.atlassian.net/browse/{epic_key}"
    ticket_lines = "\n".join(
        f"  • {t['key']} ({t['type']}, {t['points']}pts) — {t['summary'][:60]}"
        for t in created_tickets
    )
    msg = (
        f"✅ *{epic_key}* — {epic_data.get('summary', 'Epic')}\n\n"
        f"📋 *{len(created_tickets)} tickets created* ({total_points} total pts):\n"
        f"{ticket_lines}\n\n"
        f"[Open epic]({epic_link})"
    )
    bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════════════════════
# JOB 9: Morning Briefing (Telegram)
# ══════════════════════════════════════════════════════════════════════════════

def get_sprint_stats():
    """Gather current sprint stats for briefings."""
    stats = {"active_sprint": None, "total_pts": 0, "done_pts": 0, "in_progress": [], "stuck": [], "ready_count": 0}
    active = get_active_sprint()
    if not active:
        return stats
    sprint = active[0]
    stats["active_sprint"] = sprint["name"]
    sid = sprint["id"]

    issues = get_sprint_issues(sid)
    for issue in issues:
        f = issue["fields"]
        pts = f.get(STORY_POINTS_FIELD) or 0
        status = (f.get("status") or {}).get("name", "").lower()
        stats["total_pts"] += pts

        if status in COMPLETED_STATUSES:
            stats["done_pts"] += pts
        elif status == "in progress":
            # Check if stuck (updated > 3 days ago)
            updated = f.get("updated", "")
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age_days = (datetime.now(pytz.utc) - updated_dt).days
                    entry = {"key": issue["key"], "summary": f.get("summary", "")[:50], "days": age_days, "pts": pts}
                    stats["in_progress"].append(entry)
                    if age_days >= 3:
                        stats["stuck"].append(entry)
                except Exception:
                    stats["in_progress"].append({"key": issue["key"], "summary": f.get("summary", "")[:50], "days": 0, "pts": pts})
        elif status == "ready":
            stats["ready_count"] += 1
    return stats


def send_morning_briefing():
    """JOB 9: Send morning briefing to Telegram."""
    log.info("JOB 9: Morning Briefing")
    if not TELEGRAM_CHAT_ID:
        log.info("JOB 9: Skipped — no TELEGRAM_CHAT_ID.")
        return

    stats = get_sprint_stats()
    if not stats["active_sprint"]:
        send_telegram("☀️ *Morning Briefing*\n\nNo active sprint found.")
        return

    health = int((stats["done_pts"] / stats["total_pts"] * 100)) if stats["total_pts"] > 0 else 0
    remaining = stats["total_pts"] - stats["done_pts"]

    msg = f"☀️ *Morning Briefing*\n\n"
    msg += f"🏃 *Sprint:* {stats['active_sprint']}\n"
    msg += f"📊 *Health:* {health}% complete ({stats['done_pts']:.0f}/{stats['total_pts']:.0f} pts)\n"
    msg += f"📋 *Remaining:* {remaining:.0f} pts\n"
    msg += f"🔄 *In Progress:* {len(stats['in_progress'])} tickets\n"
    msg += f"📥 *Ready:* {stats['ready_count']} tickets waiting\n"

    if stats["stuck"]:
        msg += f"\n⚠️ *Stuck ({len(stats['stuck'])}):*\n"
        for t in stats["stuck"]:
            msg += f"  • {t['key']} — {t['summary']} ({t['days']}d)\n"

    # Backlog count
    try:
        backlog = get_backlog_issues()
        msg += f"\n📦 *Backlog:* {len(backlog)} tickets"
    except Exception:
        pass

    send_telegram(msg)
    log.info("JOB 9: Morning briefing sent.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 10: EOD Summary (Telegram)
# ══════════════════════════════════════════════════════════════════════════════

def send_eod_summary():
    """JOB 10: Send end-of-day summary to Telegram."""
    log.info("JOB 10: EOD Summary")
    if not TELEGRAM_CHAT_ID:
        log.info("JOB 10: Skipped — no TELEGRAM_CHAT_ID.")
        return

    stats = get_sprint_stats()
    if not stats["active_sprint"]:
        send_telegram("🌙 *EOD Summary*\n\nNo active sprint.")
        return

    health = int((stats["done_pts"] / stats["total_pts"] * 100)) if stats["total_pts"] > 0 else 0

    # Find tickets updated today
    today_start = datetime.now(pytz.timezone("Australia/Sydney")).replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d")
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": f'project = AX AND updated >= "{today_start}" ORDER BY updated DESC',
            "fields": "summary,status,assignee",
            "maxResults": 20,
        })
        moved_today = data.get("issues", [])
    except Exception:
        moved_today = []

    msg = f"🌙 *EOD Summary*\n\n"
    msg += f"🏃 *Sprint:* {stats['active_sprint']} — {health}% complete\n"
    msg += f"📊 {stats['done_pts']:.0f}/{stats['total_pts']:.0f} pts done\n"

    if moved_today:
        msg += f"\n📝 *Updated today ({len(moved_today)}):*\n"
        for issue in moved_today[:10]:
            f = issue["fields"]
            status = (f.get("status") or {}).get("name", "?")
            msg += f"  • {issue['key']} → {status} — {f.get('summary', '')[:45]}\n"
        if len(moved_today) > 10:
            msg += f"  _...and {len(moved_today) - 10} more_\n"

    if stats["stuck"]:
        msg += f"\n⚠️ *Still stuck:*\n"
        for t in stats["stuck"][:5]:
            msg += f"  • {t['key']} — In Progress for {t['days']}d\n"

    send_telegram(msg)
    log.info("JOB 10: EOD summary sent.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 11: Board Monitor — Quality Gates & Proactive Alerts
# ══════════════════════════════════════════════════════════════════════════════

def run_board_monitor():
    """JOB 11: Monitor boards for quality issues and auto-fix or alert."""
    log.info("JOB 11: Board Monitor")
    alerts = []

    # ── Auto-fix: Missing story points on non-Epic tickets ────────────────
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": f'project = AX AND issuetype not in (Epic, Subtask) AND status not in (Done, Released) AND "{STORY_POINTS_FIELD}" is EMPTY ORDER BY rank ASC',
            "fields": f"summary,issuetype,{STORY_POINTS_FIELD}",
            "maxResults": 10,
        })
        no_points = data.get("issues", [])
        for issue in no_points:
            key = issue["key"]
            summary = issue["fields"].get("summary", "")
            itype = issue["fields"]["issuetype"]["name"]
            # AI estimate
            est_prompt = f"Estimate story points for this Jira {itype}: \"{summary}\". Rules: 0.25=30min, 0.5=1hr, 1=2hrs, 2=4hrs, 3=6hrs. Max 3. Respond with ONLY a number."
            est = call_claude(est_prompt, max_tokens=10)
            if est:
                try:
                    pts = float(est.strip())
                    pts = min(max(pts, 0.25), 3)
                    ok, _ = jira_put(f"/rest/api/3/issue/{key}", {"fields": {STORY_POINTS_FIELD: pts}})
                    if ok:
                        log.info(f"  JOB 11: Auto-estimated {key} → {pts}pts")
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.warning(f"JOB 11: Story point check failed: {e}")

    # ── Auto-fix: Epics with all children Done → transition Epic to Done ──
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": 'project = AX AND issuetype = Epic AND status not in (Done, Released)',
            "fields": "summary,status",
            "maxResults": 50,
        })
        for epic in data.get("issues", []):
            epic_key = epic["key"]
            # Get children
            children_data = jira_get("/rest/api/3/search/jql", params={
                "jql": f'project = AX AND parent = {epic_key}',
                "fields": "status",
                "maxResults": 100,
            })
            children = children_data.get("issues", [])
            if children and all(
                (c["fields"].get("status") or {}).get("name", "").lower() in COMPLETED_STATUSES
                for c in children
            ):
                # All children done — transition epic
                ok, _ = jira_post(f"/rest/api/3/issue/{epic_key}/transitions", {
                    "transition": {"id": "16"}  # RELEASED
                })
                if ok:
                    log.info(f"  JOB 11: Auto-closed Epic {epic_key} — all children Done")
                    alerts.append(f"✅ Auto-closed Epic {epic_key} — all children done")
    except Exception as e:
        log.warning(f"JOB 11: Epic completion check failed: {e}")

    # ── Auto-fix: Re-enrich Partially reviewed tickets ────────────────────
    if REVIEWED_FIELD:
        try:
            data = jira_get("/rest/api/3/search/jql", params={
                "jql": 'project = AX AND Reviewed = "Partially" AND status not in (Done, Released)',
                "fields": "summary",
                "maxResults": 5,
            })
            partial = data.get("issues", [])
            if partial:
                log.info(f"  JOB 11: {len(partial)} Partially reviewed tickets found — will be caught by JOB 5")
        except Exception:
            pass

    # ── Alert: Sprint over capacity ───────────────────────────────────────
    try:
        active = get_active_sprint()
        if active:
            sid = active[0]["id"]
            total_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in get_sprint_issues(sid))
            if total_pts > MAX_SPRINT_POINTS:
                alerts.append(f"📊 Sprint at *{total_pts:.0f}/{MAX_SPRINT_POINTS}pts* — over capacity!")
    except Exception:
        pass

    # ── Alert: Stuck tickets (In Progress > 3 days) ──────────────────────
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": 'project = AX AND status = "In Progress" AND updated <= -3d',
            "fields": "summary,assignee,updated",
            "maxResults": 10,
        })
        stuck = data.get("issues", [])
        for issue in stuck:
            f = issue["fields"]
            assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
            alerts.append(f"⚠️ {issue['key']} stuck In Progress — {f.get('summary', '')[:40]} ({assignee})")
    except Exception:
        pass

    # ── Alert: PR Review with missing test plan ───────────────────────────
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": 'project = AX AND status = "PR Review"',
            "fields": "summary,description",
            "maxResults": 10,
        })
        for issue in data.get("issues", []):
            desc = issue["fields"].get("description") or ""
            desc_text = adf_to_text(desc) if isinstance(desc, dict) else str(desc)
            if "test plan" in desc_text.lower() and "tbd" in desc_text.lower().split("test plan")[-1][:100]:
                alerts.append(f"🔍 {issue['key']} in PR Review — test plan incomplete")
    except Exception:
        pass

    # ── Alert: High priority backlog tickets unactioned ───────────────────
    try:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": 'project = AX AND status in (Ready, Refine) AND priority in (Highest, High) AND created <= -2d',
            "fields": "summary,priority,created",
            "maxResults": 5,
        })
        for issue in data.get("issues", []):
            pri = (issue["fields"].get("priority") or {}).get("name", "?")
            alerts.append(f"🔥 {issue['key']} ({pri}) sitting in backlog — {issue['fields'].get('summary', '')[:40]}")
    except Exception:
        pass

    # Send consolidated alerts
    if alerts:
        log.info(f"JOB 11: {len(alerts)} alert(s) found (logged only, no Telegram).")
    else:
        log.info("JOB 11: All clear — no issues found.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 12: Archive Old Backlog Tickets (>90 days)
# ══════════════════════════════════════════════════════════════════════════════

def archive_old_backlog():
    """JOB 12: Move backlog tickets older than 90 days to the Archive project."""
    log.info("JOB 12: Archive Old Backlog")
    cutoff = (datetime.now(pytz.utc) - timedelta(days=ARCHIVE_AGE_DAYS)).strftime("%Y-%m-%d")
    archived = 0

    try:
        issues, start_at = [], 0
        while True:
            data = jira_get("/rest/api/3/search/jql", params={
                "jql": f'project = AX AND status in (Ready, Refine, Prep) AND created <= "{cutoff}" AND sprint is EMPTY ORDER BY created ASC',
                "fields": "summary,issuetype,created",
                "maxResults": 50,
                "startAt": start_at,
            })
            batch = data.get("issues", [])
            issues.extend(batch)
            if start_at + len(batch) >= data.get("total", 0):
                break
            start_at += len(batch)

        if not issues:
            log.info("JOB 12: No tickets to archive.")
            return

        log.info(f"JOB 12: Found {len(issues)} tickets older than {ARCHIVE_AGE_DAYS} days.")

        for issue in issues:
            key = issue["key"]
            itype = issue["fields"]["issuetype"]["name"]
            target_type = ARCHIVE_TYPE_MAP.get(itype, "Task")

            try:
                ok, resp = jira_put(f"/rest/api/3/issue/{key}", {
                    "fields": {
                        "project": {"key": ARCHIVE_PROJECT_KEY},
                        "issuetype": {"name": target_type},
                    }
                })
                if ok:
                    archived += 1
                    log.info(f"  Archived {key} → {ARCHIVE_PROJECT_KEY} (as {target_type})")
                else:
                    log.warning(f"  Failed to archive {key}: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                log.warning(f"  Failed to archive {key}: {e}")

        if archived > 0:
            log.info(f"JOB 12: Archived {archived} tickets to {ARCHIVE_PROJECT_KEY}.")

    except Exception as e:
        log.error(f"JOB 12 failed: {e}", exc_info=True)

    log.info(f"JOB 12: Archived {archived} tickets.")

# ══════════════════════════════════════════════════════════════════════════════
# JOB 13: Micro-Decomposition — Split tickets into 0.5-1 SP standalone tickets
# ══════════════════════════════════════════════════════════════════════════════

MICRO_LABEL = "micro-decomposed"
DECOMPOSE_MIN_SP = 2  # Only decompose tickets with SP >= this


def get_decomposable_issues():
    """Find tickets eligible for micro-decomposition: SP >= 2, no micro-decomposed label,
    in Ready/Prep/Refine status, Task/Bug/Maintenance types, not already [SPLIT]."""
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": (
                f'project = AX AND issuetype in (Task, Bug, Maintenance) '
                f'AND "Story point estimate" >= {DECOMPOSE_MIN_SP} '
                f'AND status in (Ready, Prep, Refine) '
                f'AND labels not in ("{MICRO_LABEL}") '
                f'AND summary !~ "[SPLIT]" '
                f'ORDER BY rank ASC'
            ),
            "fields": "summary,description,issuetype,priority,status,parent,assignee,"
                       f"{STORY_POINTS_FIELD},labels,issuelinks,sprint",
            "maxResults": 50,
            "startAt": start_at,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        if start_at + len(batch) >= data.get("total", 0):
            break
        start_at += len(batch)
    return issues


def build_decomposition_prompt(issue, linked_content, confluence_context):
    """Build Claude prompt for micro-decomposing a ticket into 0.5-1 SP split tickets."""
    f = issue["fields"]
    summary = f["summary"]
    desc = f.get("description") or ""
    if isinstance(desc, dict):
        desc = adf_to_text(desc)
    priority = (f.get("priority") or {}).get("name", "Medium")
    parent_summary = (f.get("parent") or {}).get("fields", {}).get("summary", "")
    sp = f.get(STORY_POINTS_FIELD) or 0
    issue_type = f["issuetype"]["name"]

    clean_desc = re.sub(r'\[?\*?\*?Definition of (Ready|Done).*$', '', desc, flags=re.DOTALL).strip()
    clean_desc = re.sub(r'!\[.*?\]\(blob:.*?\)', '[image attached]', clean_desc)

    ctx = ""
    if linked_content:
        ctx += f"\nLINKED CONTENT:\n{linked_content}\n"
    if confluence_context:
        ctx += f"\nRELATED CONFLUENCE PAGES:\n{confluence_context}\n"

    return f"""You are a senior Software Engineering Lead at Axis CRM, a life insurance distribution CRM platform.
The platform is built with Django/Python, used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.

You are splitting a Jira {issue_type} ticket into the SMALLEST possible independent tickets for smooth sprint burndown.
The goal is: each new ticket = one atomic commit/PR that can be reviewed, merged and shipped independently.
We want the burndown chart to look smooth and close to the ideal line — no large stepped drops.

TICKET: {issue["key"]}
SUMMARY: {summary}
PARENT EPIC: {parent_summary or 'None'}
PRIORITY: {priority}
CURRENT STORY POINTS: {sp}
TYPE: {issue_type}
DESCRIPTION:
{clean_desc}
{ctx}

RULES:
- Split into tickets of 0.5 or 1 story points each. 0.5 = ~1 hour of work, 1 = ~2 hours.
- Each ticket MUST be independently shippable — a single commit/PR that compiles, passes tests and doesn't break the codebase.
- Order tickets in logical implementation sequence (e.g., model/migration first → backend logic → API/views → templates/UI → tests/docs).
- Think about what an engineer would actually commit separately: a migration, a new model field, view logic, a template change, admin config, permission changes, tests.
- For this Django CRM: consider separating model/migration, view/URL, template/CSS, admin config, permission, test, and documentation changes.
- Summaries should be specific and actionable (e.g., "Add can_view_calls field to UserRole model + migration" not "Update model").
- Each ticket needs 1-3 clear acceptance criteria.
- The sum of all ticket story points should equal or be close to the original {sp} SP.
- Minimum 2 tickets, no maximum but be practical.
- Do NOT include test-only tickets unless the testing is substantial (>1hr). Instead, include relevant tests within each ticket's scope.
- For {issue_type} type tickets, write summaries as actionable engineering tasks.

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "split_tickets": [
    {{
      "summary": "<specific actionable ticket title>",
      "story_points": <0.5 or 1>,
      "acceptance_criteria": ["<criterion 1>", "<criterion 2>"],
      "sequence": <1, 2, 3...>
    }}
  ],
  "decomposition_rationale": "<1 sentence explaining why this breakdown makes sense>"
}}"""


def create_micro_split_ticket(original_issue, split_data, issue_type, sequence_num, total_count, original_key):
    """Create a standalone split ticket (same type as original), linked to the same parent epic."""
    f = original_issue["fields"]
    ac = split_data.get("acceptance_criteria", [])
    ac_str = "\n".join(f"- [ ] {c}" for c in ac) if ac else "- [ ] TBD"

    if issue_type == "Task":
        desc_md = f"""**Product Manager:**
1. **Summary:** Split {sequence_num}/{total_count} from {original_key}
2. **User story:** {split_data.get('summary', '')}
3. **Acceptance criteria:**
{ac_str}
4. **Test plan:**

**Engineer:**
1. **Technical plan:**
2. **Story points estimated:** {split_data.get('story_points', 0.5)}
3. **Task broken down (<=3 story points or split into parts):** Yes

{DOR_DOD_TASK}"""
    elif issue_type == "Bug":
        desc_md = f"""**Product Manager:**
1. **Summary:** Split {sequence_num}/{total_count} from {original_key} — {split_data.get('summary', '')}
2. **Acceptance criteria:**
{ac_str}

**Engineer:**
1. **Investigation:**

{DOR_DOD_TASK}"""
    else:  # Maintenance
        desc_md = f"""**Product Manager:**
1. **Summary:** Split {sequence_num}/{total_count} from {original_key} — {split_data.get('summary', '')}
2. **Acceptance criteria:**
{ac_str}

**Engineer:**
1. **Task:**

{DOR_DOD_TASK}"""

    payload = {
        "fields": {
            "project": {"key": "AX"},
            "summary": split_data["summary"],
            "issuetype": {"name": issue_type},
            STORY_POINTS_FIELD: float(split_data.get("story_points", 0.5)),
            "description": {"version": 1, "type": "doc", "content": markdown_to_adf(desc_md)},
        }
    }
    if REVIEWED_FIELD:
        payload["fields"][REVIEWED_FIELD] = "Yes"

    # Inherit parent epic
    if f.get("parent"):
        payload["fields"]["parent"] = {"key": f["parent"]["key"]}
    if f.get("assignee"):
        payload["fields"]["assignee"] = {"accountId": f["assignee"]["accountId"]}
    if f.get("priority"):
        payload["fields"]["priority"] = {"name": f["priority"]["name"]}

    ok, r = jira_post("/rest/api/3/issue", payload)
    if ok:
        new_key = r.json().get("key", "?")
        log.info(f"    Created {new_key}: {split_data['summary']} ({split_data.get('story_points', 0.5)}SP)")
        # Move to same sprint as original
        sprint_data = f.get("sprint")
        if sprint_data and sprint_data.get("id"):
            move_issue_to_sprint(new_key, sprint_data["id"])
        return new_key
    else:
        log.error(f"    Failed to create split ticket: {r.status_code} {r.text[:300]}")
        return None


def micro_decompose_tickets():
    """JOB 13: Split tickets >= 2 SP into 0.5-1 SP standalone tickets for smooth burndown."""
    if not ANTHROPIC_API_KEY:
        log.info("JOB 13 skipped — ANTHROPIC_API_KEY not set.")
        return

    issues = get_decomposable_issues()
    if not issues:
        log.info("JOB 13: No tickets eligible for micro-decomposition.")
        return

    log.info(f"JOB 13: Found {len(issues)} ticket(s) to micro-decompose.")

    # Limit per run to avoid API overload
    max_per_run = 10
    processed = 0

    for issue in issues[:max_per_run]:
        key = issue["key"]
        f = issue["fields"]
        issue_type = f["issuetype"]["name"]
        summary = f["summary"]
        sp = f.get(STORY_POINTS_FIELD) or 0

        log.info(f"  Decomposing {key} ({sp}SP {issue_type}): {summary}")

        # Gather context from epic, linked issues, Confluence
        linked_content = fetch_linked_content(issue)
        confluence_context = search_confluence_for_context(summary)

        # Call Claude for decomposition
        prompt = build_decomposition_prompt(issue, linked_content, confluence_context)
        response = call_claude(prompt, max_tokens=3000)

        if not response:
            log.warning(f"  Skipping {key} — Claude decomposition failed.")
            continue

        # Parse response
        try:
            clean = re.sub(r'^```(?:json)?\s*', '', response)
            clean = re.sub(r'\s*```$', '', clean)
            decomposition = json.loads(clean)
        except json.JSONDecodeError as e:
            log.warning(f"  Skipping {key} — JSON parse error: {e}")
            log.debug(f"  Response: {response[:500]}")
            continue

        split_tickets = decomposition.get("split_tickets", [])
        rationale = decomposition.get("decomposition_rationale", "")

        if len(split_tickets) < 2:
            log.info(f"  Skipping {key} — decomposition returned fewer than 2 tickets.")
            continue

        # Sort by implementation sequence
        split_tickets.sort(key=lambda x: x.get("sequence", 0))

        # Create standalone split tickets
        created_keys = []
        total_sp = 0
        for i, st in enumerate(split_tickets, 1):
            new_key = create_micro_split_ticket(issue, st, issue_type, i, len(split_tickets), key)
            if new_key:
                created_keys.append(new_key)
                total_sp += float(st.get("story_points", 0.5))

        if not created_keys:
            log.warning(f"  Skipping {key} — no split tickets were created successfully.")
            continue

        # Archive the original — move to ARU project since it's been replaced by split tickets
        target_type = ARCHIVE_TYPE_MAP.get(issue_type, "Task")
        ok, resp = jira_put(f"/rest/api/3/issue/{key}", {
            "fields": {
                "project": {"key": ARCHIVE_PROJECT_KEY},
                "issuetype": {"name": target_type},
            }
        })
        if ok:
            log.info(f"  Archived {key} → {ARCHIVE_PROJECT_KEY}")
        else:
            log.warning(f"  Failed to archive {key}: {resp.status_code if resp else 'no response'} — adding label instead")
            # Fallback: just label it so it's not reprocessed
            jira_put(f"/rest/api/3/issue/{key}", {
                "update": {"labels": [{"add": MICRO_LABEL}]}
            })

        processed += 1
        log.info(f"  Completed {key} → {len(created_keys)} tickets ({total_sp}SP total).")

    log.info(f"JOB 13: Micro-decomposed {processed} ticket(s).")

    if processed > 0:
        log.info(f"JOB 13: Split {processed} ticket(s) into standalone tickets.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 15: Strategic Pipeline — AR Ideas → Roadmap → Delivery Epics → Sprints
# ══════════════════════════════════════════════════════════════════════════════

DELIVERY_LINK_TYPE_ID = None  # Auto-discovered at startup


def discover_delivery_link_type():
    """Find the issue link type for JPD Delivery links."""
    global DELIVERY_LINK_TYPE_ID
    try:
        data = jira_get("/rest/api/3/issueLinkType")
        for lt in data.get("issueLinkTypes", []):
            if lt["name"].lower() in ("delivery", "delivers"):
                DELIVERY_LINK_TYPE_ID = lt["id"]
                log.info(f"Discovered Delivery link type: id={DELIVERY_LINK_TYPE_ID} ({lt['name']})")
                return
        # Fallback: use "Relates" if Delivery not found
        for lt in data.get("issueLinkTypes", []):
            if lt["name"].lower() == "relates":
                DELIVERY_LINK_TYPE_ID = lt["id"]
                log.info(f"No 'Delivery' link type found — using 'Relates' (id={DELIVERY_LINK_TYPE_ID})")
                return
        log.warning("No suitable link type found for strategic pipeline delivery links.")
    except Exception as e:
        log.error(f"Failed to discover delivery link type: {e}")


def get_strategic_ideas_scored():
    """Fetch all Strategic Initiatives ideas that have all 4 RICE scores."""
    jql = (
        f'project = {AR_PROJECT_KEY} AND cf[10694] = "Strategic Initiatives"'
        f' AND cf[10526] is not EMPTY AND cf[10047] is not EMPTY'
        f' AND cf[10527] is not EMPTY AND cf[10058] is not EMPTY'
    )
    fields = (
        f"summary,description,status,priority,issuelinks,"
        f"{RICE_REACH_FIELD},{RICE_IMPACT_FIELD},{RICE_CONFIDENCE_FIELD},"
        f"{RICE_EFFORT_FIELD},{ROADMAP_FIELD},{SWIMLANE_FIELD}"
    )
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search/jql", params={
            "jql": jql, "fields": fields, "maxResults": 100, "startAt": start_at
        })
        batch = data.get("issues", [])
        total = data.get("total", 0)
        issues.extend(batch)
        if start_at + len(batch) >= total:
            break
        start_at += len(batch)
    return issues


def prioritise_strategic_ideas(issues):
    """Re-prioritise scored Strategic Initiatives ideas across Roadmap columns by RICE value."""
    if not issues:
        log.info("  JOB 15: No scored Strategic Initiatives ideas to prioritise.")
        return

    for issue in issues:
        f = issue["fields"]
        r = f.get(RICE_REACH_FIELD) or 1
        i = f.get(RICE_IMPACT_FIELD) or 1
        c = f.get(RICE_CONFIDENCE_FIELD) or 1
        e = f.get(RICE_EFFORT_FIELD) or 1
        issue["_rice_value"] = (r * i * c) / e
    issues.sort(key=lambda x: x["_rice_value"], reverse=True)

    log.info(f"  JOB 15: Prioritising {len(issues)} Strategic Initiatives ideas across roadmap columns.")

    idx = 0
    for col in ROADMAP_COLUMNS:
        if idx >= len(issues):
            break
        slots = IDEAS_PER_COLUMN
        assigned = 0
        while idx < len(issues) and assigned < slots:
            issue = issues[idx]
            current_roadmap = (issue["fields"].get(ROADMAP_FIELD) or {}).get("id")
            target_id = col["id"]
            if current_roadmap != target_id:
                ok, resp = jira_put(f"/rest/api/3/issue/{issue['key']}", {
                    "fields": {ROADMAP_FIELD: {"id": target_id}}
                })
                if ok:
                    log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) → {col['value']}")
                else:
                    log.warning(f"    Failed to move {issue['key']} to {col['value']}: {resp.status_code}")
            else:
                log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) already in {col['value']}")
            idx += 1
            assigned += 1

    while idx < len(issues):
        issue = issues[idx]
        current_roadmap = (issue["fields"].get(ROADMAP_FIELD) or {}).get("id")
        if current_roadmap != ROADMAP_BACKLOG_ID:
            ok, resp = jira_put(f"/rest/api/3/issue/{issue['key']}", {
                "fields": {ROADMAP_FIELD: {"id": ROADMAP_BACKLOG_ID}}
            })
            if ok:
                log.info(f"    {issue['key']} (RICE={issue['_rice_value']:.1f}) → Backlog")
        idx += 1


def get_idea_delivery_epic(idea_links):
    """Check if an AR idea already has a linked AX delivery Epic. Returns the epic key or None."""
    for link in (idea_links or []):
        for direction in ("outwardIssue", "inwardIssue"):
            linked = link.get(direction)
            if not linked:
                continue
            linked_key = linked.get("key", "")
            linked_type = linked.get("fields", {}).get("issuetype", {}).get("name", "")
            if linked_key.startswith("AX-") and linked_type == "Epic":
                return linked_key
    return None


def build_delivery_epic_prompt(idea):
    """Build Claude prompt to generate an AX delivery Epic + child tickets from an AR idea."""
    f = idea["fields"]
    summary = f.get("summary", "")
    desc = f.get("description") or ""
    if isinstance(desc, dict):
        desc = adf_to_text(desc)

    rice_r = f.get(RICE_REACH_FIELD) or "?"
    rice_i = f.get(RICE_IMPACT_FIELD) or "?"
    rice_c = f.get(RICE_CONFIDENCE_FIELD) or "?"
    rice_e = f.get(RICE_EFFORT_FIELD) or "?"

    return f"""You are a senior Product Manager for Axis CRM, a life insurance distribution CRM platform.
The platform is used by AFSL-licensed insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.
The CRM serves multiple divisions: LIP (lead intake & processing) team, services team, and advisers.

You are creating a delivery Epic in the AX (Sprints) project from a strategic initiative idea.

SOURCE IDEA: {idea["key"]}
SUMMARY: {summary}
RICE: Reach={rice_r}, Impact={rice_i}, Confidence={rice_c}, Effort={rice_e}
DESCRIPTION:
{desc[:4000]}

Create a delivery Epic and break it into shippable tickets.

Respond with ONLY a JSON object (no markdown, no backticks):

{{
  "epic": {{
    "summary": "Implementation-focused epic title (different from idea title)",
    "description_summary": "One paragraph: what will be built and expected outcome",
    "priority": "Medium"
  }},
  "tickets": [
    {{
      "type": "Task",
      "summary": "Clear task summary",
      "priority": "Medium",
      "story_points": 2,
      "user_story": "As a [user], I want [goal] so that [benefit]",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "test_plan": ["Test step 1", "Test step 2"]
    }},
    {{
      "type": "Spike",
      "summary": "Investigation: [what]",
      "priority": "High",
      "story_points": 1,
      "investigation_summary": "What to investigate"
    }}
  ]
}}

RULES:
- At least 2 tickets under the epic.
- Epic summary must be implementation-focused, NOT a copy of the idea title.
- Types: Task, Bug, Spike, Support, Maintenance.
- Tasks MUST include user_story, acceptance_criteria (array), test_plan (array).
- Bug/Spike/Support need summary + investigation_summary. Maintenance just summary.
- Max 3 story points per ticket (3=6hrs, 2=4hrs, 1=2hrs, 0.5=1hr, 0.25=30min).
- story_points: 0.25, 0.5, 1, 2, or 3.
- Priority: Lowest, Low, Medium, High, Highest.
- Write substantive descriptions a developer can act on."""


def link_idea_to_epic(idea_key, epic_key):
    """Create an issue link between AR idea and AX delivery Epic."""
    if not DELIVERY_LINK_TYPE_ID:
        log.warning(f"  JOB 15: No delivery link type — cannot link {idea_key} → {epic_key}")
        return False
    ok, resp = jira_post("/rest/api/3/issueLink", {
        "type": {"id": DELIVERY_LINK_TYPE_ID},
        "inwardIssue": {"key": idea_key},
        "outwardIssue": {"key": epic_key},
    })
    if ok:
        log.info(f"  JOB 15: Linked {idea_key} → {epic_key}")
    else:
        log.warning(f"  JOB 15: Failed to link {idea_key} → {epic_key}: {resp.status_code} {resp.text[:200]}")
    return ok


def parse_roadmap_column(col_value):
    """Parse 'March (S1)' → (month_number, sprint_number). Returns (None, None) on failure."""
    import calendar
    m = re.match(r'(\w+)\s*\(S(\d+)\)', col_value)
    if not m:
        return None, None
    month_name = m.group(1)
    sprint_num = int(m.group(2))
    month_map = {v: k for k, v in enumerate(calendar.month_name) if v}
    return month_map.get(month_name), sprint_num


def find_sprint_for_column(col_value, all_sprints):
    """Find the sprint matching a roadmap column like 'March (S1)'. Returns sprint dict or None."""
    month_num, sprint_num = parse_roadmap_column(col_value)
    if not month_num:
        return None
    month_sprints = []
    for sprint in all_sprints:
        start_str = sprint.get("startDate", "")[:10]
        if not start_str:
            continue
        start_date = datetime.strptime(start_str, "%Y-%m-%d")
        if start_date.month == month_num:
            month_sprints.append(sprint)
    month_sprints.sort(key=lambda s: s["startDate"])
    if sprint_num <= len(month_sprints):
        return month_sprints[sprint_num - 1]
    return None


def get_column_name(roadmap_col_id):
    """Look up column name from its ID."""
    for col in ROADMAP_COLUMNS:
        if col["id"] == roadmap_col_id:
            return col["value"]
    return None


def process_strategic_pipeline():
    """JOB 15: Unified strategic pipeline — AR ideas → roadmap → delivery Epics → child tickets → sprints."""

    # ── Step 1: Prioritise strategic ideas across roadmap columns (no AI needed) ──
    log.info("  JOB 15 Step 1: Prioritising Strategic Initiatives ideas...")
    all_ideas = get_strategic_ideas_scored()
    prioritise_strategic_ideas(all_ideas)

    # Re-fetch to get updated roadmap positions (after prioritisation)
    all_ideas = get_strategic_ideas_scored()

    # ── Build the EPIC_ROADMAP_RANK cache for JOB 3/4 ranking (no AI needed) ──
    log.info("  JOB 15: Building epic → roadmap rank cache for sprint/backlog ranking...")
    EPIC_ROADMAP_RANK.clear()
    for idea in all_ideas:
        f = idea["fields"]
        col_id = (f.get(ROADMAP_FIELD) or {}).get("id")
        col_rank = COLUMN_RANK.get(col_id, 999)
        r = f.get(RICE_REACH_FIELD) or 1
        i = f.get(RICE_IMPACT_FIELD) or 1
        c = f.get(RICE_CONFIDENCE_FIELD) or 1
        e = f.get(RICE_EFFORT_FIELD) or 1
        rice_value = (r * i * c) / e
        epic_key = get_idea_delivery_epic(f.get("issuelinks") or [])
        if epic_key:
            EPIC_ROADMAP_RANK[epic_key] = (col_rank, rice_value)
    log.info(f"  JOB 15: Cached {len(EPIC_ROADMAP_RANK)} epic(s) with roadmap ranks.")

    if not ANTHROPIC_API_KEY:
        log.info("  JOB 15: Skipping epic creation — ANTHROPIC_API_KEY not set.")
        return

    # ── Step 2+3: Create delivery Epics + child tickets for ideas in columns ──
    log.info("  JOB 15 Step 2: Creating delivery Epics for roadmap-placed ideas...")
    column_ids = {col["id"] for col in ROADMAP_COLUMNS}
    roadmap_ideas = [
        idea for idea in all_ideas
        if (idea["fields"].get(ROADMAP_FIELD) or {}).get("id") in column_ids
    ]
    log.info(f"  JOB 15: {len(roadmap_ideas)} ideas in roadmap columns.")

    all_sprints = get_active_sprint() + get_future_sprints()
    all_sprints.sort(key=lambda s: s.get("startDate", ""))

    new_epics = 0
    for idea in roadmap_ideas:
        idea_key = idea["key"]
        idea_links = idea["fields"].get("issuelinks") or []

        # Skip if already has a delivery Epic
        existing_epic = get_idea_delivery_epic(idea_links)
        if existing_epic:
            log.info(f"    {idea_key} → already has {existing_epic}")
            continue

        log.info(f"    {idea_key}: Generating delivery Epic via Claude...")
        prompt = build_delivery_epic_prompt(idea)
        response = call_claude(prompt, max_tokens=4096)
        if not response:
            log.warning(f"    {idea_key}: Claude failed — skipping.")
            continue

        try:
            clean = re.sub(r'^```(?:json)?\s*', '', response)
            clean = re.sub(r'\s*```$', '', clean)
            structured = json.loads(clean)
        except json.JSONDecodeError as e:
            log.warning(f"    {idea_key}: JSON parse error: {e}")
            continue

        # Build Epic description with template
        epic_data = structured.get("epic", {})
        f = idea["fields"]
        rice_r = f.get(RICE_REACH_FIELD) or "?"
        rice_i = f.get(RICE_IMPACT_FIELD) or "?"
        rice_c = f.get(RICE_CONFIDENCE_FIELD) or "?"
        rice_e = f.get(RICE_EFFORT_FIELD) or "?"
        rice_line = f"{rice_r}R · {rice_i}I · {rice_c}C · {rice_e}E"

        epic_desc_md = (
            f"**Product Manager:**\n"
            f"1. **Summary:** {epic_data.get('description_summary', '')}\n"
            f"2. **Validated:** No\n"
            f"3. **RICE score:** {rice_line}\n"
            f"4. **PRD:**\n"
            f"5. **Source idea:** [{idea_key}](https://axiscrm.atlassian.net/browse/{idea_key})\n\n"
            f"{DOR_DOD_EPIC}"
        )

        epic_fields = {
            "project": {"key": "AX"},
            "issuetype": {"name": "Epic"},
            "summary": epic_data.get("summary", idea["fields"]["summary"]),
            "description": {"version": 1, "type": "doc", "content": markdown_to_adf(epic_desc_md)},
            "assignee": {"accountId": ANDREJ_ID},
            "priority": {"name": epic_data.get("priority", "Medium")},
        }

        ok, resp = jira_post("/rest/api/3/issue", {"fields": epic_fields})
        if not ok:
            log.error(f"    {idea_key}: Failed to create Epic: {resp.status_code} {resp.text[:300]}")
            continue

        epic_key = resp.json().get("key", "?")
        log.info(f"    {idea_key} → created {epic_key}")
        transition_to_ready(epic_key)
        link_idea_to_epic(idea_key, epic_key)

        # Update ranking cache for new epic
        idea_col_id = (idea["fields"].get(ROADMAP_FIELD) or {}).get("id")
        idea_f = idea["fields"]
        _r = idea_f.get(RICE_REACH_FIELD) or 1
        _i = idea_f.get(RICE_IMPACT_FIELD) or 1
        _c = idea_f.get(RICE_CONFIDENCE_FIELD) or 1
        _e = idea_f.get(RICE_EFFORT_FIELD) or 1
        EPIC_ROADMAP_RANK[epic_key] = (
            COLUMN_RANK.get(idea_col_id, 999),
            (_r * _i * _c) / _e
        )

        # Create child tickets
        tickets = structured.get("tickets", [])
        child_keys = []
        total_pts = 0
        for ticket in tickets:
            ticket_type = ticket.get("type", "Task")
            if ticket_type not in ("Task", "Bug", "Spike", "Support", "Maintenance"):
                ticket_type = "Task"
            child_key = create_ax_ticket(ticket, ticket_type, parent_key=epic_key)
            if child_key:
                transition_to_ready(child_key)
                pts = ticket.get("story_points", 0) or 0
                total_pts += pts
                child_keys.append(child_key)

        log.info(f"    {epic_key}: {len(child_keys)} tickets, {total_pts} SP")

        # Move child tickets to sprint matching the roadmap column
        col_id = (idea["fields"].get(ROADMAP_FIELD) or {}).get("id")
        col_name = get_column_name(col_id)
        if col_name:
            target_sprint = find_sprint_for_column(col_name, all_sprints)
            if target_sprint:
                for ck in child_keys:
                    move_issue_to_sprint(ck, target_sprint["id"])
                log.info(f"    {epic_key}: tickets → sprint '{target_sprint['name']}' (column: {col_name})")
            else:
                log.warning(f"    No sprint found for column '{col_name}'")

        new_epics += 1

    # ── Step 4: Verify sprint assignments for existing delivery tickets ──
    log.info("  JOB 15 Step 4: Verifying sprint assignments for existing delivery tickets...")
    for idea in roadmap_ideas:
        idea_links = idea["fields"].get("issuelinks") or []
        existing_epic = get_idea_delivery_epic(idea_links)
        if not existing_epic:
            continue

        col_id = (idea["fields"].get(ROADMAP_FIELD) or {}).get("id")
        col_name = get_column_name(col_id)
        if not col_name:
            continue

        target_sprint = find_sprint_for_column(col_name, all_sprints)
        if not target_sprint:
            continue

        # Find child tickets not yet in a future/active sprint
        try:
            jql = (
                f'project = AX AND parent = {existing_epic}'
                f' AND (sprint is EMPTY OR sprint in closedSprints())'
                f' AND status not in (Done, Released)'
            )
            data = jira_get("/rest/api/3/search/jql", params={
                "jql": jql, "fields": "summary", "maxResults": 50
            })
            for issue in data.get("issues", []):
                if move_issue_to_sprint(issue["key"], target_sprint["id"]):
                    log.info(f"      {issue['key']} → sprint '{target_sprint['name']}' (epic {existing_epic})")
        except Exception as e:
            log.warning(f"    Failed to check children of {existing_epic}: {e}")

    log.info(f"  JOB 15 complete. {new_epics} new delivery Epic(s) created.")


# ══════════════════════════════════════════════════════════════════════════════
# JOB 14: Product Weekly Meeting Minutes (Confluence)
# ══════════════════════════════════════════════════════════════════════════════

WEEKLY_PARENT_PAGE_ID = "103645185"   # "Checkins" parent page
WEEKLY_SPACE_ID = "1933317"           # CAD space ID
WEEKLY_SPACE_KEY = "CAD"
pending_weekly_callouts = []          # Buffer for callouts added before page is created


def confluence_get(path, params=None):
    """GET request to Confluence REST API."""
    r = requests.get(f"{CONFLUENCE_BASE}{path}", auth=auth, headers=headers, params=params, timeout=30)
    if r.status_code == 200:
        return r.json()
    log.warning(f"Confluence GET {path} → {r.status_code}: {r.text[:300]}")
    return None


def confluence_post(path, payload):
    """POST request to Confluence REST API."""
    r = requests.post(f"{CONFLUENCE_BASE}{path}", auth=auth, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    log.error(f"Confluence POST {path} → {r.status_code}: {r.text[:500]}")
    return None


def confluence_put(path, payload):
    """PUT request to Confluence REST API."""
    r = requests.put(f"{CONFLUENCE_BASE}{path}", auth=auth, headers=headers, json=payload, timeout=30)
    if r.status_code == 200:
        return r.json()
    log.error(f"Confluence PUT {path} → {r.status_code}: {r.text[:500]}")
    return None


def get_latest_product_weekly_page():
    """Find the most recent Product Weekly page under Checkins."""
    data = confluence_get("/rest/api/search", params={
        "cql": f'ancestor = {WEEKLY_PARENT_PAGE_ID} AND type = page AND title ~ "Product Weekly" ORDER BY created DESC',
        "limit": 1,
    })
    if data and data.get("results"):
        page_id = data["results"][0]["content"]["id"]
        title = data["results"][0]["title"]
        return page_id, title
    return None, None


def get_page_adf(page_id):
    """Fetch a Confluence page's body in ADF format."""
    data = confluence_get(f"/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    if data:
        adf_str = data.get("body", {}).get("atlas_doc_format", {}).get("value", "")
        if adf_str:
            return json.loads(adf_str) if isinstance(adf_str, str) else adf_str
    return None


def get_sprint_details_for_weekly():
    """Get active + next sprint details for the weekly meeting page."""
    active = get_active_sprint()
    if not active:
        return {"active": None, "next": None}

    sprint = active[0]
    sid = sprint["id"]
    issues = jira_get(f"/rest/agile/1.0/sprint/{sid}/issue", params={
        "fields": f"summary,status,issuetype,parent,{STORY_POINTS_FIELD}",
        "maxResults": 200,
    }).get("issues", [])

    done, in_progress, ready, todo = [], [], [], []
    total_pts, done_pts = 0, 0
    for i in issues:
        f = i["fields"]
        pts = f.get(STORY_POINTS_FIELD) or 0
        status = (f.get("status") or {}).get("name", "")
        total_pts += pts
        entry = {"key": i["key"], "summary": f.get("summary", ""), "pts": pts, "status": status,
                 "type": f.get("issuetype", {}).get("name", ""),
                 "epic": (f.get("parent") or {}).get("fields", {}).get("summary", "")}
        if status.lower() in COMPLETED_STATUSES:
            done.append(entry)
            done_pts += pts
        elif status.lower() == "in progress":
            in_progress.append(entry)
        elif status.lower() == "ready":
            ready.append(entry)
        else:
            todo.append(entry)

    # Next sprint
    future = get_future_sprints()
    next_sprint = None
    if future:
        ns = future[0]
        ns_issues = jira_get(f"/rest/agile/1.0/sprint/{ns['id']}/issue", params={
            "fields": f"summary,status,issuetype,parent,{STORY_POINTS_FIELD}",
            "maxResults": 200,
        }).get("issues", [])
        next_sprint = {
            "name": ns["name"],
            "start": ns.get("startDate", ""),
            "end": ns.get("endDate", ""),
            "issues": [{"key": i["key"], "summary": i["fields"].get("summary", ""),
                        "pts": i["fields"].get(STORY_POINTS_FIELD) or 0,
                        "type": i["fields"].get("issuetype", {}).get("name", ""),
                        "epic": (i["fields"].get("parent") or {}).get("fields", {}).get("summary", "")}
                       for i in ns_issues],
        }

    return {
        "active": {
            "name": sprint["name"],
            "start": sprint.get("startDate", ""),
            "end": sprint.get("endDate", ""),
            "total_pts": total_pts,
            "done_pts": done_pts,
            "done": done,
            "in_progress": in_progress,
            "ready": ready,
            "todo": todo,
        },
        "next": next_sprint,
    }


def extract_action_items_from_adf(adf):
    """Extract action items (taskItems) from the Actions row of table 1."""
    items = []
    try:
        table1 = adf["content"][1]  # First table
        actions_row = table1["content"][2]  # Third row = Actions
        actions_cell = actions_row["content"][1]  # Second cell = content

        current_person = ""
        for node in actions_cell.get("content", []):
            if node["type"] == "paragraph":
                text = ""
                for c in node.get("content", []):
                    text += c.get("text", "")
                if text.strip():
                    current_person = text.strip()
            elif node["type"] == "taskList":
                for task in node.get("content", []):
                    if task["type"] == "taskItem":
                        task_text = ""
                        for c in task.get("content", []):
                            task_text += c.get("text", "")
                        items.append({
                            "text": task_text.strip(),
                            "state": task["attrs"].get("state", "TODO"),
                            "person": current_person,
                            "localId": task["attrs"].get("localId", ""),
                        })
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to extract action items: {e}")
    return items


def update_adf_for_new_week(adf, meeting_date, sprint_data, claude_updates):
    """Modify a duplicated ADF body for the new week's meeting."""
    import copy
    new_adf = copy.deepcopy(adf)

    # 1. Update date paragraph (first node)
    try:
        date_para = new_adf["content"][0]
        for node in date_para.get("content", []):
            if node.get("type") == "date":
                # Timestamp in milliseconds
                ts = int(meeting_date.timestamp() * 1000)
                node["attrs"]["timestamp"] = str(ts)
    except (IndexError, KeyError):
        pass

    # 2. Update Actions row — carry over TODO items only
    try:
        table1 = new_adf["content"][1]
        actions_row = table1["content"][2]
        actions_cell = actions_row["content"][1]

        # Build new actions content: keep TODO items, drop DONE
        new_content = []
        current_person_node = None
        current_tasks = []

        for node in actions_cell.get("content", []):
            if node["type"] == "paragraph":
                # If we had accumulated tasks for a previous person, flush them
                if current_person_node and current_tasks:
                    new_content.append(current_person_node)
                    new_content.append({"type": "taskList", "attrs": {"localId": str(uuid4())[:12]},
                                        "content": current_tasks})
                elif current_person_node:
                    pass  # Person with no remaining tasks — skip
                current_person_node = copy.deepcopy(node)
                current_tasks = []
            elif node["type"] == "taskList":
                for task in node.get("content", []):
                    if task.get("type") == "taskItem" and task.get("attrs", {}).get("state") == "TODO":
                        current_tasks.append(copy.deepcopy(task))

        # Flush last person
        if current_person_node and current_tasks:
            new_content.append(current_person_node)
            new_content.append({"type": "taskList", "attrs": {"localId": str(uuid4())[:12]},
                                "content": current_tasks})

        # If no carried-over actions, add placeholder
        if not new_content:
            new_content = [{"type": "paragraph", "attrs": {"localId": str(uuid4())[:12]},
                            "content": [{"type": "text", "text": "No actions carried over."}]}]

        actions_cell["content"] = new_content
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to update actions: {e}")

    # 3. Update sprint goal row (row 5 of table 1)
    try:
        goal_cell = table1["content"][5]["content"][1]  # Second cell of sprint goal row
        goal_text = claude_updates.get("sprint_goal", "")
        if goal_text:
            goal_cell["content"] = [{"type": "paragraph", "attrs": {"localId": str(uuid4())[:12]},
                                     "content": [{"type": "text", "text": goal_text}]}]
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to update sprint goal: {e}")

    # 4. Update Insights row (row 0 of table 2) — add sprint progress summary
    try:
        table2 = new_adf["content"][2]
        insights_cell = table2["content"][0]["content"][1]  # Second cell of Insights row
        progress_text = claude_updates.get("insights", "Sprint progress update will be added during the meeting.")
        progress_nodes = []
        for para in progress_text.split("\n\n"):
            if para.strip():
                progress_nodes.append({"type": "paragraph", "attrs": {"localId": str(uuid4())[:12]},
                                        "content": [{"type": "text", "text": para.strip()}]})
        if progress_nodes:
            insights_cell["content"] = progress_nodes
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to update insights: {e}")

    # 5. Reset Blocked row (row 1 of table 2)
    try:
        blocked_cell = table2["content"][1]["content"][1]  # Second cell of Blocked row
        blocked_cell["content"] = [{"type": "bulletList", "attrs": {"localId": str(uuid4())[:12]},
                                    "content": [{"type": "listItem", "attrs": {"localId": str(uuid4())[:12]},
                                                  "content": [{"type": "paragraph",
                                                               "attrs": {"localId": str(uuid4())[:12]},
                                                               "content": [{"type": "text", "text": "N/A "}]}]}]}]
    except (IndexError, KeyError):
        pass

    return new_adf


def build_weekly_update_prompt(sprint_data, last_page_title):
    """Build Claude prompt to generate sprint goal and insights for the weekly page."""
    active = sprint_data.get("active")
    next_sp = sprint_data.get("next")

    active_summary = "No active sprint."
    if active:
        pct = int(active["done_pts"] / active["total_pts"] * 100) if active["total_pts"] > 0 else 0
        done_list = "\n".join(f"  ✅ {t['key']}: {t['summary']} ({t['pts']}SP)" for t in active["done"])
        ip_list = "\n".join(f"  🔄 {t['key']}: {t['summary']} ({t['pts']}SP)" for t in active["in_progress"])
        ready_list = "\n".join(f"  📋 {t['key']}: {t['summary']} ({t['pts']}SP)" for t in active["ready"])
        active_summary = f"""ACTIVE SPRINT: {active['name']}
Progress: {pct}% ({active['done_pts']:.0f}/{active['total_pts']:.0f} SP)
Start: {active['start'][:10] if active['start'] else 'N/A'} → End: {active['end'][:10] if active['end'] else 'N/A'}

Done ({len(active['done'])}):
{done_list or '  (none)'}

In Progress ({len(active['in_progress'])}):
{ip_list or '  (none)'}

Ready ({len(active['ready'])}):
{ready_list or '  (none)'}"""

    next_summary = "No upcoming sprint."
    if next_sp:
        next_issues = "\n".join(f"  📌 {t['key']}: {t['summary']} ({t['pts']}SP) [{t['epic'] or 'No epic'}]"
                                for t in next_sp["issues"][:15])
        next_summary = f"""NEXT SPRINT: {next_sp['name']}
Start: {next_sp['start'][:10] if next_sp['start'] else 'N/A'} → End: {next_sp['end'][:10] if next_sp['end'] else 'N/A'}
Tickets ({len(next_sp['issues'])}):
{next_issues}"""

    return f"""You are preparing the weekly product meeting notes for Axis CRM.
Axis CRM is a life insurance distribution CRM platform built with Django/Python, used by AFSL-licensed insurance advisers.

Generate two things:

1. SPRINT_GOAL: A concise one-line sprint goal using an emoji colour indicator and a brief description.
Use these emoji indicators:
- 🟢 On track / healthy
- 🟡 Minor risks / slightly behind
- 🔴 Blocked / significantly behind
Example: "🟢 Complete payments dashboard v1.1 and begin adviser payments view."

2. INSIGHTS: A 2-3 paragraph progress summary covering:
- What was completed this sprint (key deliverables)
- What's currently in progress
- What's coming in the next sprint
- Any notable patterns or risks

Keep it professional but conversational — this is for a leadership audience.

{active_summary}

{next_summary}

Previous meeting: {last_page_title}

RESPOND IN EXACTLY THIS JSON FORMAT (no markdown fences):
{{
  "sprint_goal": "<emoji + one-line goal>",
  "insights": "<2-3 paragraph summary>"
}}"""


def generate_product_weekly():
    """JOB 14: Generate weekly product meeting Confluence page."""
    log.info("JOB 14: Generating Product Weekly page...")

    sydney_tz = pytz.timezone("Australia/Sydney")
    now = datetime.now(sydney_tz)
    meeting_date = now.date()
    page_title = f"{meeting_date.strftime('%Y-%m-%d')} Product Weekly"

    # Check if page already exists for this date
    existing = confluence_get("/rest/api/search", params={
        "cql": f'ancestor = {WEEKLY_PARENT_PAGE_ID} AND type = page AND title = "{page_title}"',
        "limit": 1,
    })
    if existing and existing.get("results"):
        log.info(f"JOB 14: Page '{page_title}' already exists. Skipping.")
        return existing["results"][0]["content"]["id"]

    # Get the most recent Product Weekly page to duplicate
    last_page_id, last_title = get_latest_product_weekly_page()
    if not last_page_id:
        log.error("JOB 14: No previous Product Weekly page found to duplicate.")
        return None

    log.info(f"JOB 14: Duplicating from '{last_title}' (ID: {last_page_id})")

    # Get ADF body
    adf = get_page_adf(last_page_id)
    if not adf:
        log.error("JOB 14: Failed to fetch ADF body.")
        return None

    # Get sprint data
    sprint_data = get_sprint_details_for_weekly()

    # Call Claude for sprint goal and insights
    claude_updates = {"sprint_goal": "", "insights": ""}
    if ANTHROPIC_API_KEY:
        prompt = build_weekly_update_prompt(sprint_data, last_title)
        response = call_claude(prompt, max_tokens=1500)
        if response:
            try:
                clean = re.sub(r'^```(?:json)?\s*', '', response)
                clean = re.sub(r'\s*```$', '', clean)
                claude_updates = json.loads(clean)
            except json.JSONDecodeError as e:
                log.warning(f"JOB 14: Claude JSON parse error: {e}")

    # Update ADF for new week
    meeting_dt = datetime.combine(meeting_date, datetime.min.time())
    new_adf = update_adf_for_new_week(adf, meeting_dt, sprint_data, claude_updates)

    # Create the page via Confluence v2 API
    payload = {
        "spaceId": WEEKLY_SPACE_ID,
        "status": "current",
        "title": page_title,
        "parentId": WEEKLY_PARENT_PAGE_ID,
        "body": {
            "representation": "atlas_doc_format",
            "value": json.dumps(new_adf),
        },
    }
    result = confluence_post("/api/v2/pages", payload)
    if result:
        new_page_id = result.get("id", "?")
        web_url = result.get("_links", {}).get("webui", "")
        full_url = f"{CONFLUENCE_BASE}{web_url}" if web_url else f"{JIRA_BASE_URL}/wiki/spaces/{WEEKLY_SPACE_KEY}/pages/{new_page_id}"
        log.info(f"JOB 14: Created '{page_title}' — {full_url}")

        # Inject any buffered callouts from Telegram
        if pending_weekly_callouts:
            log.info(f"JOB 14: Injecting {len(pending_weekly_callouts)} buffered callout(s)...")
            for callout in pending_weekly_callouts:
                add_callout_to_weekly(new_page_id, callout, already_polished=True)
            pending_weekly_callouts.clear()

        send_telegram(
            f"📋 *Product Weekly* created for {meeting_date.strftime('%d %b %Y')}:\n"
            f"{full_url}\n\n"
            f"Use /productweekly to review actions and add callouts."
        )
        return new_page_id
    else:
        log.error("JOB 14: Failed to create Product Weekly page.")
        return None


def get_current_weekly_page():
    """Find this week's Product Weekly page (most recent one)."""
    page_id, title = get_latest_product_weekly_page()
    return page_id, title


def polish_callout(raw_text):
    """Use Claude to polish raw Telegram text into professional meeting-ready language."""
    prompt = (
        "Rewrite the following rough note into a concise, professional callout suitable for a Product Weekly meeting page. "
        "Keep it brief (1-2 sentences max). Preserve all key facts, names, and specifics. "
        "Do not add any preamble or explanation — just return the polished text.\n\n"
        f"Raw note: {raw_text}"
    )
    polished = call_claude(prompt, max_tokens=200)
    return polished.strip() if polished else raw_text


def add_callout_to_weekly(page_id, callout_text, already_polished=False):
    """Add a callout/note to the Insights section of the current Product Weekly page."""
    # Polish the raw text into professional language
    if not already_polished:
        callout_text = polish_callout(callout_text)
    adf = get_page_adf(page_id)
    if not adf:
        return False

    import copy
    new_adf = copy.deepcopy(adf)

    try:
        table2 = new_adf["content"][2]
        insights_cell = table2["content"][0]["content"][1]  # Second cell of Insights row

        # Prepend the callout as a highlighted paragraph at the top
        callout_node = {
            "type": "paragraph",
            "attrs": {"localId": str(uuid4())[:12]},
            "content": [
                {"type": "text", "text": "📢 ", "marks": []},
                {"type": "text", "text": callout_text, "marks": [{"type": "strong"}]},
            ]
        }
        insights_cell["content"].insert(0, callout_node)
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to add callout: {e}")
        return False

    # Get current version for update
    page_data = confluence_get(f"/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    if not page_data:
        return False
    version = page_data.get("version", {}).get("number", 1)

    result = confluence_put(f"/api/v2/pages/{page_id}", {
        "id": page_id,
        "status": "current",
        "title": page_data.get("title", ""),
        "spaceId": WEEKLY_SPACE_ID,
        "body": {
            "representation": "atlas_doc_format",
            "value": json.dumps(new_adf),
        },
        "version": {"number": version + 1, "message": "Added callout via Telegram"},
    })
    if result is not None:
        return callout_text  # Return polished text
    return None


def tick_action_item(page_id, item_index):
    """Mark an action item as DONE on the Product Weekly page."""
    adf = get_page_adf(page_id)
    if not adf:
        return False

    import copy
    new_adf = copy.deepcopy(adf)

    try:
        table1 = new_adf["content"][1]
        actions_cell = table1["content"][2]["content"][1]

        # Find all taskItems
        task_count = 0
        for node in actions_cell.get("content", []):
            if node["type"] == "taskList":
                for task in node.get("content", []):
                    if task.get("type") == "taskItem":
                        if task_count == item_index:
                            task["attrs"]["state"] = "DONE"
                            break
                        task_count += 1
    except (IndexError, KeyError) as e:
        log.warning(f"Failed to tick action item: {e}")
        return False

    page_data = confluence_get(f"/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    if not page_data:
        return False
    version = page_data.get("version", {}).get("number", 1)

    result = confluence_put(f"/api/v2/pages/{page_id}", {
        "id": page_id,
        "status": "current",
        "title": page_data.get("title", ""),
        "spaceId": WEEKLY_SPACE_ID,
        "body": {
            "representation": "atlas_doc_format",
            "value": json.dumps(new_adf),
        },
        "version": {"number": version + 1, "message": "Action item completed via Telegram"},
    })
    return result is not None


def run():
    log.info("=== Starting Jira prioritisation run ===")
    try:
        log.info("JOB 0: Sprint Lifecycle")
        manage_sprint_lifecycle()

        log.info("JOB 1: Sprint Runway")
        future_sprints = get_future_sprints()
        future_sprints = ensure_sprint_runway(future_sprints, required=8)

        log.info("JOB 15: Strategic Pipeline")
        process_strategic_pipeline()

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

        log.info("JOB 6: Process User Feedback Ideas")
        process_user_feedback()

        log.info("JOB 11: Board Monitor")
        run_board_monitor()

        log.info("JOB 12: Archive Old Backlog")
        archive_old_backlog()

        log.info("JOB 13: Micro-Decomposition")
        micro_decompose_tickets()

        # JOB 14 runs on its own Friday schedule, not in the core loop

        log.info("=== Run complete ===")
    except Exception as e:
        log.error(f"Run failed: {e}", exc_info=True)


if __name__ == "__main__":
    import threading

    sydney_tz = pytz.timezone("Australia/Sydney")
    scheduler = BlockingScheduler(timezone=sydney_tz)

    # Core jobs run every 30 minutes during work hours (7am-6pm Mon-Fri)
    scheduler.add_job(
        run,
        trigger=CronTrigger(day_of_week="mon-fri", hour="7-17", minute="0,30", timezone=sydney_tz),
        id="core_loop",
        name="Core 30-min loop (7am-5:30pm)",
    )

    # Morning briefing — 7:30am Mon-Fri
    scheduler.add_job(
        send_morning_briefing,
        trigger=CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=sydney_tz),
        id="morning_briefing",
        name="Morning Briefing",
    )

    # EOD summary — 5:00pm Mon-Fri
    scheduler.add_job(
        send_eod_summary,
        trigger=CronTrigger(day_of_week="mon-fri", hour=17, minute=30, timezone=sydney_tz),
        id="eod_summary",
        name="EOD Summary",
    )

    # Product Weekly — 7:00am Friday
    scheduler.add_job(
        generate_product_weekly,
        trigger=CronTrigger(day_of_week="fri", hour=7, minute=0, timezone=sydney_tz),
        id="product_weekly",
        name="Product Weekly (Friday 7am)",
    )

    log.info("Scheduler started — core loop every 30min (7am-6pm), briefing 7:30am, EOD 5:30pm, Product Weekly Fri 7am AEDT.")
    discover_reviewed_field()
    discover_delivery_link_type()

    # Start Telegram bot in a daemon thread (runs alongside scheduler)
    if TELEGRAM_BOT_TOKEN:
        tg_thread = threading.Thread(target=start_telegram_bot, daemon=True)
        tg_thread.start()
        log.info("Telegram bot thread started.")
    else:
        log.info("Telegram bot skipped — TELEGRAM_BOT_TOKEN not set.")

    run()
    scheduler.start()
