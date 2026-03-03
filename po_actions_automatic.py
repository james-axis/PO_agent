"""
PO Agent — Automatic Scheduled Actions
Sprint lifecycle approval, retrospective generation, and other automated triggers.
"""

import json
import re
import copy
import logging
from datetime import datetime
from uuid import uuid4
import pytz

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
RETRO_PARENT_PAGE_ID = "103645185"   # "Checkins" parent page (same as Product Weekly)

# Attendee account IDs for retro @mentions
ATTENDEES = [
    {"accountId": "5cf3eb99a6e4d50e24901e17", "name": "Dave Kuhn"},
    {"accountId": "712020:00983fc3-e82b-470b-b141-77804c9be677", "name": "Andrej Kudriavcev"},
    {"accountId": "712020:bc32a9de-a5bf-446a-bd4f-26091c942202", "name": "Dvir"},
    {"accountId": "712020:b28bb054-a469-4a9f-bfde-0b93ad1101ae", "name": "James Nicholls"},
]

# Pending sprint close approvals: {sprint_id: {sprint, incomplete, carryover_target, ...}}
pending_sprint_approvals = {}


# ══════════════════════════════════════════════════════════════════════════════
# SPRINT LIFECYCLE WITH TELEGRAM APPROVAL
# ══════════════════════════════════════════════════════════════════════════════

def check_sprint_lifecycle():
    """JOB 0 replacement: detect expired sprints and ask for approval via Telegram.
    Returns True if a sprint needs closing (approval pending), False if nothing to do."""
    from main import get_active_sprint, get_future_sprints, get_incomplete_issues, send_telegram, \
        COMPLETED_STATUSES, STORY_POINTS_FIELD, TELEGRAM_CHAT_ID

    sydney_tz = pytz.timezone("Australia/Sydney")
    today = datetime.now(sydney_tz).date()

    active_sprints = get_active_sprint()
    if not active_sprints:
        # No active sprint — check if we need to start one
        _maybe_auto_start_sprint()
        return False

    for sprint in active_sprints:
        end = datetime.strptime(sprint["endDate"][:10], "%Y-%m-%d").date()
        sid = sprint["id"]

        # Already pending approval for this sprint?
        if sid in pending_sprint_approvals:
            continue

        if end <= today:
            # Sprint has ended — gather data and ask for approval
            incomplete = get_incomplete_issues(sid)
            # Get completed issues for summary
            from main import get_sprint_issues
            all_issues = get_sprint_issues(sid)
            completed = [i for i in all_issues if i["fields"]["status"]["name"].lower() in COMPLETED_STATUSES]

            total_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in all_issues)
            done_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in completed)
            incomplete_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in incomplete)

            # Find next sprint
            future = get_future_sprints()
            next_sprint = future[0] if future else None

            pending_sprint_approvals[sid] = {
                "sprint": sprint,
                "incomplete": incomplete,
                "completed": completed,
                "next_sprint": next_sprint,
                "total_pts": total_pts,
                "done_pts": done_pts,
                "incomplete_pts": incomplete_pts,
            }

            # Build the approval message
            sprint_name = sprint["name"]
            inc_count = len(incomplete)
            done_count = len(completed)
            next_name = next_sprint["name"] if next_sprint else "None"

            inc_list = ""
            if incomplete:
                inc_items = [f"  • {i['key']} — {i['fields'].get('summary', '?')}" for i in incomplete[:8]]
                if inc_count > 8:
                    inc_items.append(f"  ... +{inc_count - 8} more")
                inc_list = "\n" + "\n".join(inc_items)

            msg = (
                f"🏁 *Sprint ending: {sprint_name}*\n\n"
                f"✅ Completed: {done_count} tickets ({done_pts:.0f} pts)\n"
                f"⚠️ Incomplete: {inc_count} tickets ({incomplete_pts:.0f} pts){inc_list}\n"
                f"📊 Velocity: {done_pts:.0f}/{total_pts:.0f} pts\n\n"
                f"Next sprint: *{next_name}*\n"
                f"Incomplete tickets will carry over.\n\n"
                f"Reply:\n"
                f"  /approve\\_sprint — Close & start next\n"
                f"  /hold\\_sprint — Keep current sprint open"
            )

            send_telegram(msg)
            log.info(f"JOB 0: Sprint '{sprint_name}' expired — approval requested via Telegram.")
            return True

    return False


def approve_sprint_close():
    """Handle sprint close approval. Close the expired sprint, carry over incomplete, start next."""
    from main import close_sprint, start_sprint, move_issue_to_sprint, send_telegram

    if not pending_sprint_approvals:
        return "❌ No sprint pending approval."

    # Process the first (should be only) pending approval
    sid, data = next(iter(pending_sprint_approvals.items()))
    sprint = data["sprint"]
    incomplete = data["incomplete"]
    next_sprint = data["next_sprint"]
    done_pts = data["done_pts"]

    sprint_name = sprint["name"]

    # Close the sprint
    if not close_sprint(sid):
        return f"❌ Failed to close sprint '{sprint_name}'."

    log.info(f"JOB 0: Closed sprint '{sprint_name}' (approved).")

    # Start next sprint and carry over
    carryover_msg = ""
    if next_sprint:
        if start_sprint(next_sprint):
            log.info(f"JOB 0: Started sprint '{next_sprint['name']}'.")
            carried = 0
            for issue in incomplete:
                if move_issue_to_sprint(issue["key"], next_sprint["id"]):
                    carried += 1
            if carried:
                carryover_msg = f"\n🔄 {carried} ticket(s) carried over to {next_sprint['name']}."
        else:
            carryover_msg = f"\n❌ Failed to start '{next_sprint['name']}'."
    else:
        carryover_msg = "\n⚠️ No future sprint to start."

    pending_sprint_approvals.pop(sid, None)

    result = (
        f"✅ Sprint *{sprint_name}* closed.\n"
        f"📊 Velocity: {done_pts:.0f} pts{carryover_msg}"
    )
    send_telegram(result)
    return result


def hold_sprint():
    """Cancel the sprint close request — keep current sprint open."""
    from main import send_telegram

    if not pending_sprint_approvals:
        return "❌ No sprint pending approval."

    sid, data = next(iter(pending_sprint_approvals.items()))
    sprint_name = data["sprint"]["name"]
    pending_sprint_approvals.pop(sid, None)

    msg = f"⏸ Sprint *{sprint_name}* held open. Will check again next run."
    send_telegram(msg)
    log.info(f"JOB 0: Sprint '{sprint_name}' held open (user request).")
    return msg


def _maybe_auto_start_sprint():
    """If no active sprint exists and no pending approval, auto-start the next future sprint."""
    from main import get_future_sprints, start_sprint, send_telegram

    if pending_sprint_approvals:
        return  # Waiting for approval

    future = get_future_sprints()
    if future:
        ns = future[0]
        if start_sprint(ns):
            log.info(f"JOB 0: Auto-started sprint '{ns['name']}' (no active sprint).")
            send_telegram(f"🏃 Sprint *{ns['name']}* auto-started (no active sprint detected).")


# ══════════════════════════════════════════════════════════════════════════════
# RETROSPECTIVE PAGE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_retrospective():
    """JOB 18: Generate sprint retrospective Confluence page.
    Duplicates previous retro template, fills with AI-generated insights from sprint data.
    Triggered when sprint close is approved.
    """
    from main import (confluence_get, confluence_post, get_page_adf, get_active_sprint,
                      get_sprint_issues, call_claude, send_telegram,
                      WEEKLY_SPACE_ID, CONFLUENCE_BASE, JIRA_BASE_URL,
                      COMPLETED_STATUSES, STORY_POINTS_FIELD)

    log.info("JOB 18: Generating Retrospective page...")

    sydney_tz = pytz.timezone("Australia/Sydney")

    # Get the closing/most recent active sprint for dates
    active = get_active_sprint()
    if not active:
        log.info("JOB 18: No active sprint — skipping retro generation.")
        return None

    sprint = active[0]
    start_date = sprint.get("startDate", "")[:10]
    end_date = sprint.get("endDate", "")[:10]

    if not start_date or not end_date:
        log.warning("JOB 18: Sprint missing dates — skipping.")
        return None

    # Format dates as DD/MM/YY
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    title = f"{start_dt.strftime('%d/%m/%y')} - {end_dt.strftime('%d/%m/%y')} sprint retro summary"

    # Check if retro page already exists
    existing = confluence_get("/rest/api/search", params={
        "cql": f'ancestor = {RETRO_PARENT_PAGE_ID} AND type = page AND title = "{title}"',
        "limit": 1,
    })
    if existing and existing.get("results"):
        log.info(f"JOB 18: Retro page '{title}' already exists. Skipping.")
        page = existing["results"][0]["content"]
        web_url = f"{JIRA_BASE_URL}/wiki{page['_links']['webui']}"
        return page["id"], web_url

    # Get sprint data for AI analysis
    all_issues = get_sprint_issues(sprint["id"])
    completed = [i for i in all_issues if i["fields"]["status"]["name"].lower() in COMPLETED_STATUSES]
    incomplete = [i for i in all_issues if i["fields"]["status"]["name"].lower() not in COMPLETED_STATUSES]
    total_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in all_issues)
    done_pts = sum((i["fields"].get(STORY_POINTS_FIELD) or 0) for i in completed)

    # Build sprint summary for Claude
    completed_summary = "\n".join([
        f"  - {i['key']}: {i['fields'].get('summary', '?')} ({i['fields'].get('issuetype', {}).get('name', '?')}, "
        f"{i['fields'].get(STORY_POINTS_FIELD) or 0} pts)"
        for i in completed[:20]
    ])
    incomplete_summary = "\n".join([
        f"  - {i['key']}: {i['fields'].get('summary', '?')} ({i['fields'].get('status', {}).get('name', '?')})"
        for i in incomplete[:10]
    ])

    sprint_goal = sprint.get("goal", "No goal set")

    # Generate AI retro insights
    retro_content = _generate_retro_ai_content(
        sprint_name=sprint["name"],
        sprint_goal=sprint_goal,
        velocity=f"{done_pts:.0f}/{total_pts:.0f}",
        completed_summary=completed_summary,
        incomplete_summary=incomplete_summary,
        done_count=len(completed),
        incomplete_count=len(incomplete),
    )

    # Build ADF for the retro page
    adf = _build_retro_adf(retro_content, sprint)

    # Create the page
    payload = {
        "spaceId": WEEKLY_SPACE_ID,
        "status": "current",
        "title": title,
        "parentId": RETRO_PARENT_PAGE_ID,
        "body": {
            "representation": "atlas_doc_format",
            "value": json.dumps(adf),
        },
    }

    result = confluence_post("/api/v2/pages", payload)
    if result:
        new_page_id = result.get("id", "?")
        web_url = result.get("_links", {}).get("webui", "")
        full_url = f"{CONFLUENCE_BASE}{web_url}" if web_url else f"{JIRA_BASE_URL}/wiki/spaces/CAD/pages/{new_page_id}"
        log.info(f"JOB 18: Created retro '{title}' — {full_url}")

        send_telegram(
            f"📝 *Sprint Retrospective* created for {sprint['name']}:\n"
            f"{full_url}\n\n"
            f"📊 Velocity: {done_pts:.0f}/{total_pts:.0f} pts "
            f"({len(completed)} done, {len(incomplete)} incomplete)"
        )
        return new_page_id, full_url
    else:
        log.error("JOB 18: Failed to create retro page.")
        send_telegram("❌ Failed to create retrospective page. Check logs.")
        return None


def _generate_retro_ai_content(sprint_name, sprint_goal, velocity, completed_summary,
                                incomplete_summary, done_count, incomplete_count):
    """Use Claude to generate retro good/bad/actions from sprint data."""
    from main import call_claude

    prompt = (
        "You are analysing a sprint retrospective for a small product development team "
        "(Product Owner, Engineer, QA). Generate retrospective content based on the sprint data below.\n\n"
        f"Sprint: {sprint_name}\n"
        f"Sprint Goal: {sprint_goal}\n"
        f"Velocity: {velocity} story points\n"
        f"Completed ({done_count} tickets):\n{completed_summary}\n\n"
        f"Incomplete ({incomplete_count} tickets):\n{incomplete_summary}\n\n"
        "Return a JSON object with:\n"
        "- \"good\": list of 3-5 strings (what went well — based on tickets completed, themes, velocity)\n"
        "- \"improve\": list of 3-5 strings (what could be improved — based on incomplete work, patterns)\n"
        "- \"actions\": list of 1-3 strings (specific action items for next sprint)\n\n"
        "Be specific and reference actual ticket themes. Keep each item to 1 sentence.\n"
        "Return ONLY valid JSON, no preamble or markdown."
    )

    response = call_claude(prompt, max_tokens=800)
    if not response:
        return {"good": [], "improve": [], "actions": []}

    try:
        clean = re.sub(r'^```(?:json)?\s*', '', response.strip())
        clean = re.sub(r'\s*```$', '', clean)
        return json.loads(clean)
    except json.JSONDecodeError as e:
        log.warning(f"JOB 18: Claude JSON parse error: {e}")
        return {"good": [], "improve": [], "actions": []}


def _build_retro_adf(retro_content, sprint):
    """Build ADF document for the retro page."""
    good_items = retro_content.get("good", [])
    improve_items = retro_content.get("improve", [])
    actions = retro_content.get("actions", [])

    # Attendees paragraph with @mentions
    attendee_nodes = [{"type": "text", "text": "Attendees: ", "marks": [{"type": "strong"}]}]
    for i, att in enumerate(ATTENDEES):
        attendee_nodes.append({
            "type": "mention",
            "attrs": {"id": att["accountId"], "text": f"@{att['name']}",
                      "accessLevel": "", "userType": "DEFAULT"},
        })
        if i < len(ATTENDEES) - 1:
            attendee_nodes.append({"type": "text", "text": " "})

    # Build good/improve table rows
    # Header row
    header_row = {
        "type": "tableRow",
        "content": [
            {"type": "tableHeader", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "What went well (Good)", "marks": [{"type": "strong"}]}
                ]}
            ]},
            {"type": "tableHeader", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "What could be improved (Bad / Could be better)", "marks": [{"type": "strong"}]}
                ]}
            ]},
        ]
    }

    # Data rows — pair good and improve items, pad shorter list
    max_rows = max(len(good_items), len(improve_items), 5)
    data_rows = []
    for idx in range(max_rows):
        good_text = good_items[idx] if idx < len(good_items) else ""
        improve_text = improve_items[idx] if idx < len(improve_items) else ""
        data_rows.append({
            "type": "tableRow",
            "content": [
                {"type": "tableCell", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": good_text}] if good_text else []}
                ]},
                {"type": "tableCell", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": improve_text}] if improve_text else []}
                ]},
            ]
        })

    table = {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
             "content": [header_row] + data_rows}

    # Actions section
    actions_content = []
    for action in actions:
        actions_content.append({
            "type": "taskList",
            "attrs": {"localId": str(uuid4())[:8]},
            "content": [{
                "type": "taskItem",
                "attrs": {"localId": str(uuid4())[:8], "state": "TODO"},
                "content": [{"type": "text", "text": action}],
            }]
        })
    if not actions_content:
        actions_content = [{"type": "paragraph", "content": []}]

    # Other discussion section (empty)
    other_content = [{"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": []}]}
    ]}]

    # Full ADF document
    adf = {
        "version": 1,
        "type": "doc",
        "content": [
            # Attendees
            {"type": "paragraph", "content": attendee_nodes},
            # Separator
            {"type": "rule"},
            # Good/Bad table
            table,
            # Actions heading
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Actions"}]},
            *actions_content,
            # Other discussion heading
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Other discussion"}]},
            *other_content,
        ]
    }

    return adf


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMAND REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

def register_commands(bot):
    """Register /approve_sprint and /hold_sprint commands on the PO Agent Telegram bot."""

    @bot.message_handler(commands=["approve_sprint"])
    def handle_approve_sprint(message):
        result = approve_sprint_close()
        # Generate retro page after approval
        try:
            retro_result = generate_retrospective()
            if not retro_result:
                log.info("JOB 18: Retro generation skipped or failed after sprint close.")
        except Exception as e:
            log.error(f"JOB 18: Retro generation error after sprint close: {e}")

    @bot.message_handler(commands=["hold_sprint"])
    def handle_hold_sprint(message):
        hold_sprint()

    log.info("Registered /approve_sprint and /hold_sprint commands.")
