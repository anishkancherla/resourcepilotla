# Architecture

ResourcePilot is eight independent uAgents that share one Postgres database. The Orchestrator owns
the ASI:One chat and every workflow. The seven specialists each own one step and answer only over
messages. All eight run from `agents/run.py`, one process per agent, selected by `AGENT_ROLE`.

| Key | Agent | Port | Job in the workflow |
| --- | --- | --- | --- |
| `orchestrator` | ResourcePilot Orchestrator | 8000 | Chat, menu, cards, workflow state, re-check and reroute |
| `intake` | ResourcePilot Intake | 8001 | Form fields to a validated brief; keeps only the school domain |
| `researcher` | ResourcePilot Campus Researcher | 8002 | School, room-booking system, housing pages, and events feed from the domain |
| `campus_rooms` | ResourcePilot Campus Rooms | 8003 | Open study or wellness rooms read live from LibCal; the live re-check of one room |
| `verification` | ResourcePilot Verification | 8004 | Checks room offers; re-opens official housing links; re-fetches the events feed |
| `planner` | ResourcePilot Planner | 8005 | Ranks open rooms; replans without a taken room; matches housing to criteria |
| `memory_audit` | ResourcePilot Memory and Audit | 8006 | Append-only audit timeline |
| `emergency` | ResourcePilot Emergency Dispatch | 8007 | Places one demo-drill call to your team's demo contact, never 911, and reports the transcript |

## Protocols

| Protocol | Who speaks it | Messages |
| --- | --- | --- |
| Agent Chat Protocol | Every agent | `ChatMessage`, `ChatAcknowledgement` |
| `ResourcePilotTasks` 1.0.0 | Every agent | `TaskRequest`, `TaskResult`, `AuditEvent` |

Every agent publishes both manifests. A specialist's profile chat answers with a status card that
names its role and points to the Orchestrator; only the Orchestrator starts workflows.

## Services and their chains

The Orchestrator answers every chat with a card. A greeting or any free text gets the services
menu; there is no sentence parser. Each menu button opens a form, and each form submission starts
a workflow for one service.

| Service | Specialist chain | Ends with |
| --- | --- | --- |
| `rooms` | Intake → Campus Researcher → Campus Rooms → Verification → Planner | A rooms carousel, then the re-check and a booking link |
| `wellness` | Intake → Campus Researcher → Campus Rooms → Verification → Planner | A wellness card: crisis line, open wellness rooms, the week's events (status `awaiting_selection`) |
| `wellness`, no room system | Intake → Campus Researcher → Verification | The same card without rooms (status `answered`) |
| `housing` | Intake → Campus Researcher → Verification → Planner | A halls carousel, or the housing facts card when no hall could be read (status `answered`) |
| `emergency`, preview | Emergency Dispatcher (`phase="preview"`) | A review card with **Call now** and **Cancel** (status `awaiting_call`) |
| `emergency`, call | Emergency Dispatcher (`phase="call"`) | A call card with **Check the call** (status `called`) |

A chain that ends at Verification is an answer (`answered`), and so is housing; a room search that
ends at the Planner has options to re-check (`awaiting_selection`).

**My bookings** starts no chain. The Orchestrator reads your five most recent workflows whose
status is `booked` from the shared store and shows them as one card.

Before any workflow exists, the Orchestrator reduces every email field to its domain
(`scrub_fields`). The local part of the address is never stored, sent, or logged.

### Campus Researcher

The Researcher matches the domain to a school using the IPEDS directory. The directory is about
15MB, so it is fetched once, compacted to a domain index, and cached for a week. A department
subdomain matches its parent domain, and branch campuses that share a parent domain name the
system without claiming a campus. A non-`.edu` domain that IPEDS does not know is refused. If
IPEDS cannot be read, the request fails with a retryable error rather than guessing a timezone.

For rooms it then finds the booking system, stopping at the first hit:

1. a hand-checked list (for example `ucla.edu`);
2. `<school>.libcal.com/allspaces`, accepted only when the page links back to the school;
3. a crawl of `lib.`, `library.`, `libraries.`, and similar starting pages. The crawl goes at most
   two hops, follows room-booking links, and recognizes LibCal, EMS, 25Live, Skedda, and Robin. A
   LibCal site is confirmed by its `/allspaces` page.

If none of these finds a system, the workflow pauses at `awaiting_link`, and the Orchestrator
shows a card with one link field. A pasted link goes back to the Researcher, which checks it with
the same rules. A link that fails the checks returns the same card with the reason.

For housing it reads IPEDS institutional characteristics and finds the housing office page. The
portal must be a link on the office's own pages, or on its housing vendor (StarRez), that reads
like an application or portal. It then reads the halls (`resourcepilot/housing.py`):

1. the listing page: a hand-checked page for a few schools (`KNOWN_HOUSING`), else the office's own
   links scored for "housing options", "residence halls", "where to live", "apartments", "rates";
2. hall links on it: text that names a place (Hall, House, Towers, Apartments, Village, ...) or a
   page nested under the listing, minus noise (associations, contracts, staff, faculty, seasonal
   housing) and the site's home page; a category link ("Residence Halls") is read as another
   listing;
3. each hall page, six at a time within 28 fetches and 30 seconds: from its `<main>` content with
   menus, headers, and footers removed, the name, an `og:image` on the school's domain, style, who
   it is for, room types, features, the lowest listed price annualized, and a short "about".

Then prices (`resourcepilot/rates.py`), best first for each hall:

1. the hall's own page;
2. the school's rates page (a hand-checked page for a few schools, else the office's links that
   read "rates", "costs", "fees", "billing"): a table row with an amount whose cells name the
   hall, lowest regular row wins. Tables under a summer, study-abroad, transition, or fees heading
   or tab are skipped. The period comes from the cell, the column header, or the sentence just
   above the table; with none, the amount is shown as listed and never annualized. Rows that name
   a room type but no hall are kept as school-wide rates;
3. the hall's rate PDF, linked under the hall's name: the yearly row ("Annual cost", "Academic
   year cost"), only its room columns (those the header names before any dining plan), as a
   range.

Pricing has its own budget (16 fetches, 15 seconds, at most 10 PDFs of 3MB). With no hall price,
cards show the school's IPEDS typical cost from the newest year it reported (2023, then 2022):
typical room charge, else room and board combined.

Results are cached per domain. A school whose halls cannot be read gets the facts card.

For wellness it finds the room system the same way but never pauses for a link: rooms are
optional on the wellness card. It also looks for the school's public Localist calendar at
`events.<domain>/api/2/events`, then `calendar.<domain>`, then `<school>.localist.com` (accepted
only when its home page links back to the school). It keeps up to eight events in the next seven
days whose title, keywords, tags, or filter names read as health or wellness, with only a capped
title, start time, location, and link. A missing or failing feed is shown on the card, never a
failed request.

Results are cached per domain in `api_cache`: a week when found, a day otherwise. A week's event
list is cached for 30 minutes. The fetch rules are in [trust-model.md](trust-model.md).

### Campus Rooms

Campus Rooms receives the school context the Researcher found (`campus`: domain, school name,
timezone, booking system, booking URL, and LibCal host) and the brief's time window.

- **LibCal schools.** It reads the public room list from `/allspaces` (cached for a day), keeps
  study spaces and leaves out equipment and special-purpose spaces, drops rooms too small for the
  group, and reads open slots from the availability grid that LibCal's public booking page calls
  (cached for two minutes). Each offer is the first opening that fits your duration inside your
  window.
- **Other systems** (EMS, 25Live, Skedda, Robin, or a plain booking page) get one offer that links
  to the school's booking page, because their availability is not public. If LibCal's live times
  fail, the offer falls back to that same link.
- **Nothing fits.** It returns one unavailable offer that says so ("No room for 4 at … is open for
  60 minutes in that window"), which Verification turns into a clear error card.

- **Wellness.** It reads only wellness spaces (meditation, reflection, prayer, wellness,
  lactation, rest, or sensory), which are never offered as study rooms, for one person. With no
  LibCal, no wellness space listed, or none open, it returns one unavailable offer with the
  reason instead of a link.

Campus Rooms reads only a LibCal host that is on the school's own domain or `libcal.com`, and only
after checking that the host resolves to public addresses.

### Verification and Planner

Verification checks every room offer: it must have a name and a room ID, be available, have a
booking link that is https on the school's domain or a known booking vendor, have a slot inside
the requested window when it has live times, and, when the seat count is known, fit the group.
Rejected offers are kept with their reasons. If no offer passes, the workflow fails with the first
reason.

For housing, Verification re-opens the office and portal links and every hall page itself and
drops any that do not open. The Planner then rates each hall against each criterion as `match`,
`unknown` (the page does not say), or `out` (the page rules it out): who it is for (transfer
students also fit continuing halls), style, each must-have (a missing feature is unknown, never
out), and budget. It returns `halls` (no `out`, matches first, then fewest unknowns, then lowest
yearly price, at most ten), `all_halls`, and `hidden_count`.

For wellness, a search with no acceptable room is not a failure: Verification passes the first
reason on as `room_note`, and the Planner returns no options instead of an error. Verification
also re-fetches the events feed itself and drops events it no longer lists. Picking a wellness
room uses the same live re-check and booking link as a study room; a room taken during the
re-check returns to the wellness card without it.

The Planner turns the accepted offers into up to six room options, earliest opening first and then
the snuggest fit for the group. The first option is labeled **Earliest opening**. Each option has
a stable `option_id` derived from the room ID and its start time, so a button press always names
the same room and slot.

### Emergency Dispatcher

The emergency service is a demo drill. Its form asks what kind of emergency it is (medical, fire or
smoke, someone in danger, or other), where on campus, how many people need help, optional details,
whether you are safe, and an optional school name. Its chain is only the Emergency Dispatcher.

1. **Preview.** The Orchestrator sends the Dispatcher a `phase="preview"` task. The Dispatcher
   validates the form, removes email addresses, caps free text, and returns the review data: the
   masked demo number ("•••• 1234"), "not 911", the voice's opening line, and the facts. The
   review card offers **Call now** and **Cancel**, and the workflow waits at `awaiting_call`.
2. **Call.** The Orchestrator claims the **Call now** press once per workflow, then sends a
   `phase="call"` task. The Dispatcher also claims the call once per workflow and places exactly
   one ElevenLabs Twilio outbound call to `EMERGENCY_DEMO_TO_NUMBER`. It is never retried. The
   call card (status `called`) offers **Check the call**.
3. **Status.** **Check the call** sends a `phase="status"` task. The Dispatcher reads the call's
   status, duration, summary, and transcript from ElevenLabs, and the Orchestrator shows them.

Only the configured number is ever dialed; a number in the form or task payload is ignored.
Emergency, crisis, and short numbers are refused, and the voice always opens with "this is a
ResourcePilot demo drill, not a real emergency". Every emergency card says it is a demo drill that
does not contact 911 or campus police, and "In a real emergency, call 911." A call starts only from
the **Call now** selection, never from prose. The rules are in [trust-model.md](trust-model.md).

## The task pipeline

```text
Orchestrator ─TaskRequest─▶ Intake            ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Campus Researcher ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Campus Rooms      ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Verification      ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Planner           ─TaskResult─▶ Orchestrator ─▶ rooms carousel

Orchestrator ─TaskRequest─▶ Emergency Dispatcher ─TaskResult─▶ Orchestrator ─▶ emergency card
```

Each step is a `ctx.send` from the Orchestrator to the next specialist's address. Each
`TaskRequest` carries the workflow ID, a fresh request ID, the requester, the session, the target
role, the payload, and a deadline (`TASK_TIMEOUT_SECONDS`, 45 by default). A specialist ignores a
task from any sender other than the Orchestrator and refuses a task that is past its deadline.

The Orchestrator accepts a `TaskResult` only when all of these hold:

- The sender is the registered address of the claimed source role.
- The result is the first one seen for that request ID.
- The request ID is the one the workflow currently expects.
- The workflow is at that role's stage.

Late, duplicate, and forged results are ignored. Each accepted result becomes the workflow's
latest data (`brief`, `school`, `rooms`, `campus`, `offers`, `rejections`, `options`,
`exclude_items`, `housing`), and the Orchestrator sends the next task in the chain.

The Orchestrator also sends an `AuditEvent` to Memory and Audit for every workflow start,
specialist result, retry, timeout, re-check, reroute, and booking. Memory and Audit accepts events
only from ResourcePilot's own agents and appends each one to the timeline once per event ID.

## The re-check and the reroute

The carousel shows each option with **View room**. The room detail card offers **Re-check this
room** and **Other rooms**.

1. **Re-check.** The Orchestrator claims the press once, so a double click never sends two
   re-checks. It stores the chosen option, moves the workflow to the `recheck` stage, and sends
   Campus Rooms a correlated `TaskRequest` with `phase="recheck"`, the school context, and that
   room line. Campus Rooms reads LibCal's availability for that exact slot again, uncached. A
   booking-page offer has no live times, so it is passed through as a link, and the link card says
   it was not checked live.
2. **Still open.** The workflow's status becomes `booked`, and the Orchestrator sends the booking
   link card: the room, the time, the school's link, and a plain note that ResourcePilot did not
   reserve it. There is no confirm step, no hold, and no payment. The student books on the school's
   site with their own sign-in. The room then appears under **My bookings**.
3. **Taken, or not readable.** The Orchestrator tells the student why ("Someone just booked that
   time; pick another room") and sends the Planner a new task with that room's `item_id` added to
   `exclude_items`. Only that room is dropped, and a new carousel shows the remaining open rooms.
   If no room is left, the student gets an error card that says so.
4. **No answer.** If Campus Rooms does not answer the re-check before its deadline, nothing was
   handed over: the workflow goes back to the rooms carousel with a note, and the student can pick
   a room again.

## Timeouts and duplicate protection

- **Deadlines.** Every running workflow has the deadline of the task it is waiting for. The
  Orchestrator sweeps for overdue workflows on an interval.
- **One retry.** A chain task that misses its deadline is retried once with a fresh request ID.
  A second miss fails the workflow with a card that names the agent that did not answer. A
  re-check that misses its deadline returns the student to the rooms carousel instead.
- **Emergency steps.** A silent emergency preview is retried once; no call is placed at preview.
  A call step that misses its deadline is never retried: the workflow fails with a card that says
  to start a new demo, so one press can never dial twice. A silent status check returns to the
  call card with a note.
- **One-time claims.** Each inbound chat message, each task result, each timeout, each pasted
  link, each re-check press, and each **Call now** press is claimed once in Postgres before it is
  acted on, so a redelivered message or a double press cannot start the same step twice. The
  Emergency Dispatcher also claims each workflow's call once.
- **Sender checks.** Tasks are accepted only from the Orchestrator, results only from the address
  of the role they claim, and audit events only from ResourcePilot's own agents.

## Recovery

| Failure | What happens |
| --- | --- |
| A study room is taken during the re-check | Only that room is dropped (`exclude_items`), and the Planner shows the remaining open rooms |
| The Researcher finds no booking system | The workflow waits at `awaiting_link` for a pasted link, checked with the same rules |
| LibCal's availability endpoint fails | Campus Rooms offers the school's booking link only, never invented rooms |
| No room fits the window or the group | Verification rejects the unavailable offer, and the error card says why |
| A housing link no longer opens | Verification drops it, and the housing card shows only the links that opened |
| A hall page no longer opens | Verification drops that hall before the Planner sees it |
| No hall can be read from the school's site | The housing facts card, with a note that the office lists the halls |
| No hall fits the criteria | A card says so, with **Show all halls** and **Change criteria** |
| IPEDS cannot be read | The request fails with a retryable error rather than a guess |
| A specialist task is lost | The Orchestrator retries it once with a fresh request ID, then fails with a clear card |
| Campus Rooms does not answer a re-check | The rooms carousel comes back with a note, and nothing was handed over |
| The Emergency Dispatcher does not answer a preview | The preview is retried once; no call was placed |
| The Emergency Dispatcher does not answer a call | The call is never retried; the workflow fails with a card that says to start a new demo. If the answer arrives late, the call card is shown, since the phone may already be ringing |
| The Emergency Dispatcher does not answer a status check | The call card comes back with a note |
| A result arrives after the workflow moved on | It is ignored |

## State

| Table | Holds |
| --- | --- |
| `workflows` | One JSON state per workflow: service, stage, status, correlation, deadline, latest data, the chosen option |
| `workflow_events` | The append-only audit timeline written by Memory and Audit |
| `idempotency_keys` | One-time claims for chat messages, results, timeouts, pasted links, re-checks, and demo calls |
| `api_cache` | Cached public lookups (IPEDS, Researcher results, LibCal pages) with an expiry |

Workflow statuses: `running` while a specialist works, `awaiting_link` when the Researcher needs a
pasted link, `awaiting_selection` when the rooms carousel is shown, `booked` after a successful
re-check, `answered` for a housing card, `awaiting_call` when the emergency review card is shown,
`called` after the demo call is placed, and `failed` with a reason.
