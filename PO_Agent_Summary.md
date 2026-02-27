# PO Agent — Automated Product Owner for Axis CRM

## What It Does

A Python automation agent that runs the operational side of Product Ownership for the AX (Sprints) and AR (Strategic Roadmap) Jira projects. It manages sprint planning, ticket quality, board hygiene, meeting prep, and daily comms — all autonomously.

## Jobs

| # | Job | What It Does | Frequency |
|---|-----|-------------|-----------|
| 0 | Sprint Lifecycle | Auto-closes expired sprints, starts the next one, carries over incomplete tickets | Every 30min |
| 1 | Sprint Runway | Ensures 8 future 2-week sprints always exist (starting Tuesdays) | Every 30min |
| 2 | Backlog → Sprint Loading | Takes "Ready" tickets from backlog, sorted by strategic roadmap priority, packs into future sprints up to 40 SP cap | Every 30min |
| 3 | Sprint Ranking | Ranks issues within each sprint by strategic roadmap priority (column position → Vote → Jira priority fallback) | Every 30min |
| 4 | Backlog Ranking | Ranks the full backlog by strategic roadmap priority (same chain as JOB 3) | Every 30min |
| 5 | Ticket Enrichment | AI-powered: fills PM sections of descriptions using work-type templates (Epic, Task, Bug, Maintenance, Spike, Support), estimates story points (1–3 scale), splits tickets >3 SP. **Transitions tickets to Ready when Reviewed=Yes.** Skips tickets already marked Reviewed=Yes | Every 30min |
| 6 | User Feedback Ideas | Processes AR (Jira Product Discovery) ideas — scores Vote (0–5), cleans descriptions, prioritises across roadmap columns | Every 30min |
| 7 | Telegram → JPD Ideas | Voice/text messages create structured ideas in the AR roadmap with AI-estimated Vote | On demand |
| 8 | Telegram → AX Tickets | Voice/text messages create Epics with child tickets, auto-enriched and transitioned to Ready | On demand |
| 9 | Morning Briefing | Sprint progress, velocity, blockers — sent to Telegram at 7:30am | Daily 7:30am |
| 10 | EOD Summary | Day's completions, remaining work, burndown status | Daily 5pm |
| 11 | Board Monitor | Detects quality issues (missing SP, orphaned tickets, stale statuses), auto-fixes where possible, alerts via Telegram | Every 30min |
| 12 | Archive Old Backlog | Moves backlog tickets >90 days old to the ARU archive project | Every 30min |
| 13 | Micro-Decomposition | Splits tickets ≥2 SP into 0.5–1 SP standalone tickets for smoother burndown, archives the original to ARU | Every 30min |
| 14 | Product Weekly | Duplicates last week's Confluence meeting page, injects Claude-generated sprint summary and health indicator, carries over TODO action items | Fridays 7am |
| 15 | Strategic Pipeline | Unified AR→AX flow: Vote-prioritises SI ideas into roadmap columns, auto-creates delivery Epics with Claude-generated breakdown, links AR idea ↔ AX Epic, loads child tickets into matching **future sprint only** (respecting 40 SP cap and Ready status requirement). Never touches the active sprint | Every 30min |
| 16 | Sprint Rebalance & Roadmap Sync | Checks all future sprints for >40 SP. Cascades lowest-priority To Do/Ready tickets to the next future sprint. Then syncs AR idea roadmap columns to match actual sprint placement of delivery epics | Every 30min |

## Prioritisation System — Vote (0–5)

All prioritisation across AR (Strategic Roadmap) and AX (Sprints) uses a single **Vote** field (`customfield_10834`) with a 0–5 integer scale:

| Vote | Meaning |
|------|---------|
| 5 | Highest — critical need, strong revenue/compliance impact |
| 4 | High — significant need, clear business benefit |
| 3 | Medium — moderate need, reasonable benefit |
| 2 | Low — minor need, limited benefit |
| 1 | Lowest — nice-to-have, minimal impact |
| 0 | Backlog — not actionable now, park for later |
| null | Unscored — also sent to Backlog automatically |

**How Vote drives the system:**
- AR ideas sorted by Vote descending → assigned to roadmap columns (4 ideas per column)
- Vote=0 and null → automatically moved to Backlog column
- AX sprint ranking uses the linked AR idea's roadmap position → Vote → Jira priority as tiebreaker
- AI estimates Vote (0–5) when creating ideas via Telegram commands
- JOB 6 (User Feedback) has Claude score Vote during enrichment

## Dynamic Roadmap (12 Columns)

The roadmap board uses **12 rolling columns starting from the active sprint**:

- Column 1 = current active sprint (e.g. "February (S2)")
- Columns 2–12 = next 11 sprint periods rolling forward
- **4 ideas per column**
- As sprints progress, the window shifts automatically — old columns drop off, new ones are created
- Column options are auto-discovered at startup via seed data, JQL queries, and the Jira custom field context API
- After sprint rebalancing (JOB 16), idea roadmap columns are synced to match where delivery epics actually landed

## Ticket Lifecycle

```
To Do → [JOB 5 enriches] → Reviewed=Yes → Ready → [JOB 2 moves to sprint] → In Progress → Done
```

1. **To Do**: Ticket created (manually or via Telegram)
2. **JOB 5 (Enrichment)**: AI fills PM sections, estimates story points. Sets Reviewed field:
   - `Reviewed = "Yes"` → all PM fields complete → **auto-transitions to Ready**
   - `Reviewed = "Partially"` → some fields incomplete → stays in To Do, re-processed next run
   - Tickets with `Reviewed = "Yes"` are **skipped** — never re-enriched
3. **Ready**: Ticket is fully specified and waiting for sprint capacity
4. **JOB 2 (Sprint Loading)**: Moves Ready tickets into future sprints (up to 40 SP cap)
5. **JOB 16 (Rebalance)**: If a sprint exceeds 40 SP, lowest-priority tickets cascade to the next sprint

## Sprint Capacity Management

- **Cap**: 40 story points per sprint (To Do + Ready status tickets)
- **JOB 2**: Fills future sprints from Ready backlog, stops when cap reached
- **JOB 15**: Creates delivery tickets in future sprints only (never active sprint), respects SP cap
- **JOB 16**: After all loading, scans future sprints for overflows:
  - Sorts tickets by roadmap priority (highest first)
  - Bumps lowest-priority tickets to the next future sprint
  - Cascading — if the next sprint also overflows, caught on the next run
  - After rebalancing, syncs AR idea roadmap columns to match actual epic placement
- **Active sprint**: Never touched by JOB 2, 15, or 16 — only JOB 3 ranks within it

## Telegram Commands

| Command | Action |
|---------|--------|
| `/strategic` | Voice/text → AR idea with Vote (0–5) estimated by AI |
| `/feedback` | Voice/text → AR user feedback idea with Vote |
| `/backlog` | Voice/text → AX Epic + child tickets |
| `/update` | Edit existing tickets — summary, description, story points. Send ticket ID + instruction |
| `/add` | Create child tickets under an existing Epic. Validates parent, Claude structures the ticket(s) |
| `/productweekly` | Review action items, mark done, add callouts (polished by Claude) |
| Voice note | Auto-transcribed and processed based on active mode |

## Technical Setup

**Runtime:** Single `main.py` (~4,000 lines), deployed on Railway with auto-deploy from GitHub.

**Stack:**
- Python 3 + APScheduler (cron-based scheduling, Sydney timezone)
- Jira REST API v3 (direct `requests` calls with Basic Auth)
- Jira Product Discovery (JPD) for AR roadmap idea management
- Confluence REST API v2 (ADF manipulation for page creation/updates)
- Anthropic Claude API (ticket enrichment, micro-decomposition, sprint summaries, callout polishing, delivery epic generation, Vote scoring)
- Telegram Bot API via pyTelegramBotAPI (commands, voice transcription via SpeechRecognition + pydub)
- ffmpeg (voice note conversion)

**Environment Variables:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — Atlassian access
- `ANTHROPIC_API_KEY` — Claude for AI features
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — Telegram notifications and commands

**Key Custom Fields:**
- `customfield_10834` — Vote (0–5, prioritisation)
- `customfield_10560` — Roadmap column (JPD)
- `customfield_10016` — Story Points
- `customfield_10694` — Swimlane (Strategic Initiatives / User Feedback)
- `customfield_10020` — Sprint
- Reviewed field — auto-discovered at startup (three-state: empty → "Partially" → "Yes")

**Key Technical Patterns:**
- Custom field auto-discovery (Reviewed field ID and Delivery link type found at startup)
- Dynamic roadmap column sync at startup — discovers/creates 12 columns from active sprint forward
- Sprint-to-column mapping (parses "March (S1)" → first sprint starting in March)
- ADF (Atlassian Document Format) deep manipulation for Confluence pages
- Pagination handling across all Jira queries (beyond 50-item default)
- Three-state review system: empty → "Partially" → "Yes" (Yes = Ready transition)
- Graceful degradation (AI jobs skip if no API key, Telegram skips if no token)

**Schedule:** Core loop runs every 30 minutes from 7am–6pm Mon–Fri AEDT (JOBs 0–6, 11–13, 15–16). Morning briefing at 7:30am, EOD summary at 5:30pm, Product Weekly page creation Fridays at 7am.

**Job execution order:** JOB 15 (Strategic Pipeline) → JOB 2 (Backlog → Sprint) → JOB 16 (Rebalance & Roadmap Sync) → JOB 3 (Rank Sprints) → JOB 4 (Rank Backlog) → JOB 5 (Enrich) → JOB 6 (Feedback).

**Repository:** [github.com/james-axis/PO_agent](https://github.com/james-axis/PO_agent)
