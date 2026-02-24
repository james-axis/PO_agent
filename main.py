import os
import requests
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "https://axiscrm.atlassian.net")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
BOARD_ID       = os.getenv("JIRA_BOARD_ID", "1")
ANDREJ_ID      = os.getenv("ANDREJ_ID", "712020:00983fc3-e82b-470b-b141-77804c9be677")

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

def get_incomplete_issues(sprint_id):
    """Get issues from a sprint that are not Done or Released."""
    issues = get_sprint_issues(sprint_id)
    return [i for i in issues if i["fields"]["status"]["name"] not in ("Done", "Released")]

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
