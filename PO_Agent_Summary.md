# PO Agent — Automated Product Owner for Axis CRM

## What It Does

A Python automation agent that runs the operational side of Product Ownership for the AX (Sprints) and AR (Strategic Roadmap) Jira projects. It manages sprint planning, ticket quality, board hygiene, meeting prep, and daily comms — all autonomously.

The core philosophy is simple: **the roadmap is the single source of truth**. You manually prioritise ideas in roadmap columns, and the agent syncs everything downstream — delivery epics, sprint assignments, capacity enforcement, feedback alignment, and visual organisation.

---

## How It Works

```
YOU (manual)                          AGENT (automated)
───────────                           ──────────────────
Place SI ideas in                     Creates delivery Epics + child tickets
roadmap columns          ───►         Loads tickets into matching sprints
                                      Enforces 40 SP cap per sprint
                                      If over capacity → bumps tickets to next sprint
                                      AND slides the idea to the next roadmap column
                                      Aligns feedback ideas to matching SI + column
                                      Organises ideas within columns by initiative
```

### Ticket Lifecycle

```
To Do → [JOB 5 enriches] → Reviewed=Yes → Ready → [JOB 2 loads to sprint] → In Progress → Done
```

1. **To Do** — Ticket created (manually or via Telegram)
2. **JOB 5 (Enrichment)** — AI fills PM sections, estimates story points, sets Reviewed:
   - `Reviewed = "Yes"` → all PM fields complete → **auto-transitions to Ready**
   - `Reviewed = "Partially"` → incomplete → stays in To Do, re-processed next run
   - Tickets with `Reviewed = "Yes"` are **skipped** — never re-enriched
3. **Ready** — Fully specified, waiting for sprint capacity
4. **JOB 2 (Sprint Loading)** — Moves Ready tickets into future sprints (up to 40 SP cap)
5. **JOB 16 (Rebalance)** — If a sprint exceeds 40 SP, lowest-priority tickets cascade to the next sprint and the linked roadmap idea slides to the next column

### Sprint Capacity Management

- **Cap:** 40 story points per sprint (To Do + Ready status tickets counted)
- **JOB 2:** Fills future sprints from the Ready backlog, stops when cap reached
- **JOB 15:** Creates delivery tickets in **future sprints only** (never the active sprint), respects SP cap
- **JOB 16:** Scans all future sprints for overflows after loading:
  - Sorts tickets by roadmap priority (column position, earliest first)
  - Bumps lowest-priority tickets to the next future sprint
  - Cascading — if the next sprint also overflows, caught on the next run
  - After rebalancing, syncs AR idea roadmap columns to match actual sprint placement
- **Active sprint:** Never touched by JOB 2, 15, or 16 — only JOB 3 ranks within it

### Dynamic Roadmap (12 Rolling Columns)

- Column 1 = current active sprint (e.g. "February (S2)")
- Columns 2–12 = next 11 sprint periods rolling forward
- 4 ideas per column
- As sprints progress, the window shifts — old columns drop off, new ones appear
- Column options are auto-discovered at startup
- After sprint rebalancing, idea columns sync to match where delivery epics actually landed

### Feedback → Strategic Initiative Alignment

User Feedback ideas are automatically matched to their parent Strategic Initiative using AI content analysis. When matched:
- The feedback idea's initiative field is updated from "VoA" to the SI's initiative (e.g. "Payments Module · Iteration · Features")
- The feedback idea is moved to the same roadmap column as the matched SI
- This keeps both swimlanes visually aligned on the roadmap board

### Roadmap Visual Organisation (JOB 17)

Within each roadmap column, ideas are automatically sorted to create a structured visual flow:
1. **Grouped by initiative module** — all "Compliance Module" ideas together, all "Quoting Feature" together, etc.
2. **Ordered by lifecycle within each group** — Workflows → Modules → MVP → Iteration → Features

This means on the roadmap board, you see clean visual clusters of related work flowing through their natural lifecycle, rather than scattered ideas.

---

## Jobs

| # | Job | What It Does |
|---|-----|-------------|
| 0 | Sprint Lifecycle | Auto-closes expired sprints, starts the next one, carries over incomplete tickets |
| 1 | Sprint Runway | Ensures 8 future 2-week sprints always exist (starting Tuesdays) |
| 2 | Backlog → Sprint Loading | Takes Ready tickets from backlog, sorted by roadmap priority, packs into future sprints up to 40 SP cap |
| 3 | Sprint Ranking | Ranks issues within each sprint by roadmap column position → Jira priority |
| 4 | Backlog Ranking | Ranks the full backlog by roadmap column position → Jira priority |
| 5 | Ticket Enrichment | AI fills PM sections using work-type templates (Epic, Task, Bug, Maintenance, Spike, Support), estimates story points (1–3 scale), splits tickets >3 SP. Transitions to Ready when Reviewed=Yes |
| 6 | User Feedback | Cleans adviser quotes into 💬 format, then aligns each feedback idea to its matching Strategic Initiative (copies initiative values + roadmap column) |
| 7 | Telegram → JPD Ideas | Voice/text messages create structured ideas in the AR roadmap |
| 8 | Telegram → AX Tickets | Voice/text messages create Epics with child tickets, auto-enriched and transitioned to Ready |
| 9 | Morning Briefing | Sprint progress, velocity, blockers — sent to Telegram at 7:30am |
| 10 | EOD Summary | Day's completions, remaining work, burndown status — sent at 5:30pm |
| 11 | Board Monitor | Detects quality issues (missing SP, orphaned tickets, stale statuses), auto-fixes where possible |
| 12 | Archive Old Backlog | Moves backlog tickets >90 days old to the ARU archive project |
| 13 | Micro-Decomposition | Splits tickets ≥2 SP into 0.5–1 SP standalone tickets for smoother burndown, archives original to ARU |
| 14 | Product Weekly | Duplicates last week's Confluence page, injects Claude-generated sprint summary and health indicator, carries over TODO action items. Runs Fridays 7am |
| 15 | Strategic Pipeline | Reads manually-placed roadmap positions, creates delivery Epics with Claude-generated breakdown, links AR idea ↔ AX Epic, loads child tickets into matching future sprint (respecting 40 SP cap). Never touches the active sprint |
| 16 | Sprint Rebalance & Roadmap Sync | Enforces 40 SP cap across all future sprints. Bumps lowest-priority overflow tickets to the next sprint. Syncs AR idea roadmap columns to match actual epic sprint placement |
| 17 | Roadmap Organisation | Sorts ideas within each roadmap column by initiative module grouping, then by lifecycle order: Workflows → Modules → MVP → Iteration → Features |

### Execution Order (core loop)

```
JOB 0  Sprint Lifecycle
JOB 1  Sprint Runway
JOB 15 Strategic Pipeline (read roadmap → create epics → load to sprints)
JOB 2  Backlog → Sprint Loading
JOB 16 Rebalance & Roadmap Sync (enforce 40 SP → slide ideas if needed)
JOB 3  Rank Sprints
JOB 4  Rank Backlog
JOB 5  Enrich Tickets
JOB 6  User Feedback (clean + align to SI)
JOB 11 Board Monitor
JOB 12 Archive Old Backlog
JOB 13 Micro-Decomposition
JOB 17 Organise Roadmap Ideas
```

Separate schedules: JOB 9 (7:30am), JOB 10 (5:30pm), JOB 14 (Fridays 7am).

### Schedule

| Window | Frequency | Days |
|--------|-----------|------|
| **7:00am – 5:30pm** | Every 30 minutes | Mon – Fri |
| **6:00pm – 6:00am** | Every 2 hours | Mon – Fri |
| **All day** | Every 2 hours | Sat – Sun |

Morning briefing at 7:30am, EOD summary at 5:30pm, Product Weekly Fridays 7am. All times AEDT (Sydney).

---

## Prioritisation

The roadmap is **manually managed** — you place ideas in columns, and the agent reads those positions to drive everything downstream.

**Ranking chain:** Roadmap column position (earliest column = highest priority) → Jira priority as tiebreaker.

The agent traces each AX ticket back to its parent Epic, then to the linked AR idea's roadmap column. Tickets connected to roadmap ideas rank above unconnected tickets.

If capacity forces a ticket out of its target sprint, the agent cascades it to the next sprint AND slides the linked idea to the next roadmap column — keeping the roadmap and sprints in sync.

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/strategic` | Voice/text → AR idea (Strategic Initiatives swimlane) |
| `/feedback` | Voice/text → AR idea (User Feedback swimlane) |
| `/backlog` | Voice/text → AX Epic + child tickets |
| `/update` | Edit existing tickets — summary, description, story points |
| `/add` | Create child tickets under an existing Epic |
| `/productweekly` | Review action items, mark done, add callouts |
| Voice note | Auto-transcribed and processed based on active mode |

---

## Technical Setup

**Runtime:** Single `main.py` (~4,200 lines), deployed on Railway with auto-deploy from GitHub.

**Stack:**
- Python 3 + APScheduler (cron-based scheduling, Sydney timezone)
- Jira REST API v3 (direct `requests` calls with Basic Auth)
- Jira Product Discovery (JPD) for AR roadmap idea management
- Confluence REST API v2 (ADF manipulation for page creation/updates)
- Anthropic Claude API (ticket enrichment, micro-decomposition, sprint summaries, delivery epic generation, feedback-to-SI matching)
- Telegram Bot API via pyTelegramBotAPI (commands, voice transcription via SpeechRecognition + pydub)

**Environment Variables:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — Atlassian access
- `ANTHROPIC_API_KEY` — Claude for AI features
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — Telegram notifications and commands

**Key Custom Fields:**
- `customfield_10560` — Roadmap column (JPD)
- `customfield_10016` — Story Points
- `customfield_10694` — Swimlane (Strategic Initiatives / User Feedback)
- `customfield_10628` — Initiative (multi-select: module + stage + scope)
- `customfield_10020` — Sprint
- Reviewed field — auto-discovered at startup (three-state: empty → "Partially" → "Yes")

**Key Technical Patterns:**
- Custom field auto-discovery at startup (Reviewed field ID, Delivery link type)
- Dynamic roadmap column sync — discovers/creates 12 columns from active sprint forward
- Sprint-to-column mapping (parses "March (S1)" → first sprint starting in March)
- ADF (Atlassian Document Format) deep manipulation for Confluence pages
- Pagination handling across all Jira queries
- Three-state review system: empty → "Partially" → "Yes" (Yes = Ready transition)
- AI content matching for feedback → SI alignment
- Initiative lifecycle sorting (Workflows → Modules → MVP → Iteration → Features)
- Graceful degradation (AI jobs skip if no API key, Telegram skips if no token)

**Repository:** [github.com/james-axis/PO_agent](https://github.com/james-axis/PO_agent)
