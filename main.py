import os
import requests
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import re

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "https://axiscrm.atlassian.net")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
BOARD_ID       = os.getenv("JIRA_BOARD_ID", "1")
ANDREJ_ID      = os.getenv("ANDREJ_ID", "712020:00983fc3-e82b-470b-b141-77804c9be677")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

MAX_SPRINT_POINTS = 40
PRIORITY_ORDER    = {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5}

auth    = (JIRA_EMAIL, JIRA_API_TOKEN)
headers = {"Accept": "application/json", "Content-Type": "application/json"}

def get_active_sprint():
    res = requests.get(f"{JIRA_BASE_URL}/rest/agile/1.0/board/{BOARD_ID}/sprint?state=active", auth=auth, headers=headers)
    res.raise_for_status()
    return res.json().get("values", [])

def get_future_sprints():
    res = requests.get(f"{JIRA_BASE_URL}/rest/agile/1.0/board/{BOARD_ID}/sprint?state=future", auth=auth, headers=headers)
    res.raise_for_status()
    sprints = res.json().get("values", [])
    sprints.sort(key=lambda s: s["startDate"])
    return sprints

def get_sprint_issues(sprint_id):
    res = requests.get(f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}/issue", auth=auth, headers=headers, params={"fields": "summary,priority,status", "maxResults": 200})
    res.raise_for_status()
    return res.json().get("issues", [])

def get_sprint_todo_points(sprint_id):
    total = 0
    for i in get_sprint_issues(sprint_id):
        if i["fields"]["status"]["name"] == "To Do":
            total += i["fields"].get("customfield_10016") or 0
    return total

def get_andrej_ready_backlog():
    jql    = f'project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status = Ready AND status != Released AND assignee = "{ANDREJ_ID}" AND cf[10016] is not EMPTY'
    params = {"jql": jql, "fields": "summary,priority,customfield_10016", "maxResults": 200}
    res    = requests.get(f"{JIRA_BASE_URL}/rest/api/3/search/jql", auth=auth, headers=headers, params=params)
    res.raise_for_status()
    issues = res.json().get("issues", [])
    issues.sort(key=lambda i: PRIORITY_ORDER.get(i["fields"]["priority"]["name"], 999))
    return issues

def get_backlog_issues():
    params = {"jql": "project = AX AND (sprint is EMPTY OR sprint in closedSprints()) AND status != Released AND status != Done", "fields": "summary,priority,status,customfield_10020", "maxResults": 200}
    res    = requests.get(f"{JIRA_BASE_URL}/rest/api/3/search/jql", auth=auth, headers=headers, params=params)
    res.raise_for_status()
    return res.json().get("issues", [])

def move_issue_to_sprint(issue_key, sprint_id):
    res = requests.post(f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}/issue", auth=auth, headers=headers, json={"issues": [issue_key]})
    return res.status_code in (200, 204)

def rank_issues(issues, label):
    if len(issues) < 2:
        log.info(f"{label}: only {len(issues)} issue(s), no ranking needed.")
        return
    issues.sort(key=lambda i: PRIORITY_ORDER.get((i["fields"].get("priority") or {}).get("name", ""), 999))
    keys = [i["key"] for i in issues]
    log.info(f"{label} — ranking {len(keys)} issues")
    for idx in range(len(keys) - 2, -1, -1):
        res = requests.put(f"{JIRA_BASE_URL}/rest/agile/1.0/issue/rank", auth=auth, headers=headers, json={"issues": [keys[idx]], "rankBeforeIssue": keys[idx + 1]})
        if res.status_code not in (200, 204):
            log.warning(f"Failed ranking {keys[idx]}: {res.status_code} {res.text}")

def next_tuesday(dt):
    days_ahead = (1 - dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return dt + timedelta(days=days_ahead)

def create_sprint(name, start_date, end_date):
    res = requests.post(f"{JIRA_BASE_URL}/rest/agile/1.0/sprint", auth=auth, headers=headers, json={"name": name, "startDate": start_date.strftime("%Y-%m-%dT00:00:00.000Z"), "endDate": end_date.strftime("%Y-%m-%dT00:00:00.000Z"), "originBoardId": int(BOARD_ID)})
    if res.status_code in (200, 201):
        sprint = res.json()
        log.info(f"Created sprint '{name}' (id: {sprint['id']})")
        return sprint
    else:
        log.error(f"Failed to create sprint: {res.status_code} {res.text}")
        return None

def close_sprint(sprint_id):
    res = requests.post(f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}", auth=auth, headers=headers, json={"state": "closed"})
    return res.status_code in (200, 204)

def start_sprint(sprint):
    res = requests.post(f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint['id']}", auth=auth, headers=headers, json={
        "state": "active",
        "startDate": sprint["startDate"],
        "endDate": sprint["endDate"]
    })
    return res.status_code in (200, 204)

COMPLETED_STATUSES = {"done", "released"}

def get_incomplete_issues(sprint_id):
    """Get issues from a sprint that are not Done or Released."""
    issues = get_sprint_issues(sprint_id)
    return [i for i in issues if i["fields"]["status"]["name"].lower() not in COMPLETED_STATUSES]

def manage_sprint_lifecycle():
    """Close expired active sprints, carry over incomplete issues, and start the next sprint."""
    sydney_tz = pytz.timezone("Australia/Sydney")
    today = datetime.now(sydney_tz).date()

    active_sprints = get_active_sprint()
    carryover_issues = []

    for sprint in active_sprints:
        end_date = datetime.strptime(sprint["endDate"][:10], "%Y-%m-%d").date()
        if end_date <= today:
            # Collect incomplete issues before closing
            incomplete = get_incomplete_issues(sprint["id"])
            if incomplete:
                carryover_issues.extend(incomplete)
                log.info(f"Found {len(incomplete)} incomplete issue(s) in sprint '{sprint['name']}' to carry over.")

            if close_sprint(sprint["id"]):
                log.info(f"Closed sprint '{sprint['name']}' (ended {end_date}).")
            else:
                log.error(f"Failed to close sprint '{sprint['name']}'.")

    # Re-check: if no active sprint now, start the next future one
    if not get_active_sprint():
        future = get_future_sprints()
        if future:
            next_sprint = future[0]
            if start_sprint(next_sprint):
                log.info(f"Started sprint '{next_sprint['name']}'.")

                # Move carryover issues into the new active sprint
                for issue in carryover_issues:
                    key = issue["key"]
                    if move_issue_to_sprint(key, next_sprint["id"]):
                        log.info(f"Carried over {key} to sprint '{next_sprint['name']}'.")
                    else:
                        log.warning(f"Failed to carry over {key}.")
            else:
                log.error(f"Failed to start sprint '{next_sprint['name']}'.")
        else:
            log.warning("No future sprints available to start.")

def ensure_sprint_runway(future_sprints, required=8):
    if len(future_sprints) >= required:
        log.info(f"Sprint runway OK — {len(future_sprints)} future sprints exist.")
        return future_sprints
    log.info(f"Only {len(future_sprints)} future sprints. Creating up to {required}...")
    all_sprints = get_future_sprints() + get_active_sprint()
    all_sprints.sort(key=lambda s: s.get("endDate", ""))
    last_end = datetime.strptime(all_sprints[-1]["endDate"][:10], "%Y-%m-%d") if all_sprints else datetime.now()
    for _ in range(required - len(future_sprints)):
        start = next_tuesday(last_end + timedelta(days=1))
        end   = start + timedelta(days=13)
        name  = f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"
        new   = create_sprint(name, start, end)
        if new:
            future_sprints.append(new)
        last_end = end
    future_sprints.sort(key=lambda s: s["startDate"])
    return future_sprints

TEMPLATE_MARKER = "**Product Manager:**"
DOR_DOD_LINK = '[**Definition of Ready (DoR) - Task Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Ready-(DoR))   **|**   [**Definition of Done (DoD) - Task Level**](https://axiscrm.atlassian.net/wiki/spaces/CAD/pages/91062273/Delivery+process#Definition-of-Done-(DoD))'

def get_tasks_needing_enrichment():
    """Get all non-completed Task issues that don't yet have the full template."""
    jql = 'project = AX AND issuetype = Task AND status not in (Done, Released) ORDER BY rank ASC'
    params = {"jql": jql, "fields": "summary,description,priority,customfield_10016,status,parent", "maxResults": 100}
    res = requests.get(f"{JIRA_BASE_URL}/rest/api/3/search/jql", auth=auth, headers=headers, params=params)
    res.raise_for_status()
    issues = res.json().get("issues", [])
    # Filter to only those missing the full template
    return [i for i in issues if not description_has_template(i["fields"].get("description") or "")]

def description_has_template(desc):
    """Check if description already contains the full enriched template."""
    return TEMPLATE_MARKER in desc and "User story:" in desc and "Test plan:" in desc and "Technical plan:" in desc

def extract_existing_content(desc):
    """Pull out any existing summary, acceptance criteria, or other content from the current description."""
    # Strip out DoR/DoD links and image blobs for cleaner extraction
    cleaned = re.sub(r'\[?\*?\*?Definition of.*$', '', desc, flags=re.DOTALL)
    cleaned = re.sub(r'!\[.*?\]\(blob:.*?\)', '[image attached]', cleaned)
    return cleaned.strip()

def call_claude_for_enrichment(summary, existing_description, story_points, priority, parent_summary):
    """Call Claude API to generate the enriched description fields."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — skipping enrichment.")
        return None

    existing_content = extract_existing_content(existing_description)
    sp_text = str(story_points) if story_points else "Not yet estimated"
    broken_down = "Yes" if story_points and story_points <= 3 else "No" if story_points else "Not yet estimated"

    prompt = f"""You are a Product Owner for a Life Insurance distribution CRM platform (Axis CRM).
The platform is used by insurance advisers to manage clients, policies, applications, quotes, payments and commissions.
Partner insurers include TAL, Zurich, AIA, MLC Life, MetLife, Resolution Life, Integrity Life and others.

Given the following Jira Task, generate the enriched description fields. Be concise and specific to THIS task.
Use the existing content as the primary source — preserve and improve it, don't discard it.

TASK SUMMARY: {summary}
PARENT EPIC: {parent_summary or 'None'}
PRIORITY: {priority}
STORY POINTS: {sp_text}
EXISTING DESCRIPTION:
{existing_content}

Respond in EXACTLY this format (no extra text before or after):

SUMMARY: <1-2 sentence summary of what this task delivers>
USER_STORY: <As a [role], I want [action], so that [benefit]>
ACCEPTANCE_CRITERIA: <bullet points, one per line, starting with "- ">
TEST_PLAN: <numbered steps to verify the acceptance criteria are met>
TECHNICAL_PLAN: <brief technical approach — mention relevant components, APIs, DB tables if inferrable. If unsure, write "To be completed by engineer during refinement.">
"""

    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )

    if res.status_code != 200:
        log.error(f"Claude API error: {res.status_code} {res.text}")
        return None

    text = res.json()["content"][0]["text"].strip()
    return parse_claude_response(text, story_points)

def parse_claude_response(text, story_points):
    """Parse the structured response from Claude into a dict."""
    fields = {}
    patterns = {
        "summary": r"SUMMARY:\s*(.+?)(?=\nUSER_STORY:)",
        "user_story": r"USER_STORY:\s*(.+?)(?=\nACCEPTANCE_CRITERIA:)",
        "acceptance_criteria": r"ACCEPTANCE_CRITERIA:\s*(.+?)(?=\nTEST_PLAN:)",
        "test_plan": r"TEST_PLAN:\s*(.+?)(?=\nTECHNICAL_PLAN:)",
        "technical_plan": r"TECHNICAL_PLAN:\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.DOTALL)
        fields[key] = match.group(1).strip() if match else ""

    sp_text = str(story_points) if story_points else "Not yet estimated"
    broken_down = "Yes" if story_points and story_points <= 3 else ("No — needs splitting" if story_points and story_points > 3 else "Not yet estimated")
    fields["story_points_text"] = sp_text
    fields["broken_down"] = broken_down
    return fields

def build_enriched_description(fields):
    """Build the final markdown description from the enriched fields."""
    ac_lines = fields["acceptance_criteria"]
    # Convert bullet points to checkbox format
    ac_formatted = re.sub(r'^- ', '- [ ] ', ac_lines, flags=re.MULTILINE)

    return f"""**Product Manager:**
1. **Summary:** {fields['summary']}
2. **User story:** {fields['user_story']}
3. **Acceptance criteria:**
{ac_formatted}
4. **Test plan:**
{fields['test_plan']}

**Engineer:**
1. **Technical plan:** {fields['technical_plan']}
2. **Story points estimated:** {fields['story_points_text']}
3. **Task broken down (<=3 story points or split into parts):** {fields['broken_down']}

{DOR_DOD_LINK}"""

def update_issue_description(issue_key, new_description):
    """Update the description of a Jira issue using the v3 API with ADF."""
    # Build ADF from markdown — use the wiki markup approach via v2 API
    res = requests.put(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
        auth=auth,
        headers=headers,
        json={
            "update": {
                "description": [{
                    "set": {
                        "version": 1,
                        "type": "doc",
                        "content": markdown_to_adf(new_description)
                    }
                }]
            }
        },
    )
    return res.status_code in (200, 204)

def markdown_to_adf(md_text):
    """Convert simple markdown to ADF content nodes."""
    content = []
    lines = md_text.split("\n")
    for line in lines:
        line = line.rstrip()
        if not line:
            continue

        # Convert inline bold **text** to ADF marks
        inline_content = parse_inline_marks(line)
        content.append({"type": "paragraph", "content": inline_content})

    return content

def parse_inline_marks(text):
    """Parse inline markdown bold and links into ADF inline nodes."""
    nodes = []
    # Pattern to match **bold**, [text](url), and plain text
    pattern = r'(\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\)|[^*\[]+)'
    for match in re.finditer(pattern, text):
        full = match.group(0)
        if match.group(2):  # Bold
            nodes.append({"type": "text", "text": match.group(2), "marks": [{"type": "strong"}]})
        elif match.group(3) and match.group(4):  # Link
            nodes.append({"type": "text", "text": match.group(3), "marks": [{"type": "link", "attrs": {"href": match.group(4)}}]})
        else:  # Plain text
            if full.strip():
                nodes.append({"type": "text", "text": full})
    if not nodes:
        nodes.append({"type": "text", "text": text})
    return nodes

def enrich_ticket_descriptions():
    """JOB 5: Enrich Task descriptions to match the standard template."""
    if not ANTHROPIC_API_KEY:
        log.info("JOB 5 skipped — ANTHROPIC_API_KEY not set.")
        return

    tasks = get_tasks_needing_enrichment()
    if not tasks:
        log.info("No tasks need enrichment.")
        return

    log.info(f"Found {len(tasks)} task(s) needing enrichment.")

    for issue in tasks:
        key = issue["key"]
        fields = issue["fields"]
        summary = fields["summary"]
        description = fields.get("description") or ""
        story_points = fields.get("customfield_10016")
        priority = fields.get("priority", {}).get("name", "Medium")
        parent_summary = ""
        if fields.get("parent"):
            parent_summary = fields["parent"].get("fields", {}).get("summary", "")

        log.info(f"Enriching {key}: {summary}")

        enriched = call_claude_for_enrichment(summary, description, story_points, priority, parent_summary)
        if not enriched:
            log.warning(f"Skipping {key} — enrichment failed.")
            continue

        new_desc = build_enriched_description(enriched)
        if update_issue_description(key, new_desc):
            log.info(f"Updated {key} with enriched description.")
        else:
            log.warning(f"Failed to update {key}.")

def run():
    log.info("=== Starting Jira prioritisation run ===")
    try:
        # JOB 0: Sprint lifecycle (close expired, start next)
        log.info("JOB 0: Sprint Lifecycle")
        manage_sprint_lifecycle()

        # JOB 1: Sprint runway
        log.info("JOB 1: Sprint Runway")
        future_sprints = get_future_sprints()
        future_sprints = ensure_sprint_runway(future_sprints, required=8)

        # JOB 2: Move backlog to sprints
        log.info("JOB 2: Move Backlog to Sprints")
        backlog = get_andrej_ready_backlog()
        if not backlog:
            log.info("No READY backlog issues to move.")
        else:
            backlog_idx = 0
            for sprint in future_sprints:
                if backlog_idx >= len(backlog):
                    break
                sprint_id   = sprint["id"]
                sprint_name = sprint["name"]
                available   = MAX_SPRINT_POINTS - get_sprint_todo_points(sprint_id)
                log.info(f"Sprint '{sprint_name}': {available}pts available.")
                if available <= 0:
                    continue
                while backlog_idx < len(backlog) and available > 0:
                    issue = backlog[backlog_idx]
                    key   = issue["key"]
                    pts   = issue["fields"].get("customfield_10016") or 0
                    pri   = issue["fields"]["priority"]["name"]
                    if pts > available:
                        backlog_idx += 1
                        continue
                    if move_issue_to_sprint(key, sprint_id):
                        available -= pts
                        log.info(f"Moved {key} ({pts}pts) [{pri}] to '{sprint_name}'. {available}pts left.")
                    backlog_idx += 1

        # JOB 3: Rank all sprints
        log.info("JOB 3: Rank All Sprints")
        for sprint in future_sprints:
            rank_issues(get_sprint_issues(sprint["id"]), f"Sprint '{sprint['name']}'")
        for sprint in get_active_sprint():
            rank_issues(get_sprint_issues(sprint["id"]), f"Active sprint '{sprint['name']}'")

        # JOB 4: Rank backlog
        log.info("JOB 4: Rank Backlog")
        backlog_all = get_backlog_issues()
        if backlog_all:
            rank_issues(backlog_all, "Backlog")

        # JOB 5: Enrich ticket descriptions
        log.info("JOB 5: Enrich Ticket Descriptions")
        enrich_ticket_descriptions()

        log.info("=== Run complete ===")

    except Exception as e:
        log.error(f"Run failed: {e}", exc_info=True)

if __name__ == "__main__":
    sydney_tz = pytz.timezone("Australia/Sydney")
    scheduler = BlockingScheduler(timezone=sydney_tz)

    for hour, minute, name in [(7, 0, "7:00am"), (12, 0, "12:00pm"), (16, 0, "4:00pm")]:
        scheduler.add_job(
            run,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=sydney_tz),
            id=name,
            name=f"{name} Sydney Run"
        )

    log.info("Scheduler started — running at 7:00am, 12:00pm, 4:00pm AEDT Mon–Fri.")
    run()  # Fire once on startup
    scheduler.start()
