# Architecture

ResourcePilot is seven independent uAgents that share one Postgres database. The Orchestrator owns
the ASI:One chat and every workflow. The six specialists each own one step and answer only over
messages. All seven run from `agents/run.py`, one process per agent, selected by `AGENT_ROLE`.

| Key | Agent | Port | Job in the workflow |
| --- | --- | --- | --- |
| `orchestrator` | ResourcePilot Orchestrator | 8000 | Chat, menu, cards, workflow state, re-check and reroute |
| `intake` | ResourcePilot Intake | 8001 | Form fields to a validated brief; keeps only the school domain |
| `researcher` | ResourcePilot Campus Researcher | 8002 | School, room-booking system, and housing pages from the domain |
| `campus_rooms` | ResourcePilot Campus Rooms | 8003 | Open rooms read live from LibCal; the live re-check of one room |
| `verification` | ResourcePilot Verification | 8004 | Checks room offers; re-opens official housing links |
| `planner` | ResourcePilot Planner | 8005 | Ranks open rooms; replans without a taken room |
| `memory_audit` | ResourcePilot Memory and Audit | 8006 | Append-only audit timeline |

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
| `housing` | Intake → Campus Researcher → Verification | A housing card (status `answered`) |

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
like an application or portal.

Results are cached per domain in `api_cache`: a week when found, a day otherwise. The fetch rules
are in [trust-model.md](trust-model.md).

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

Campus Rooms reads only a LibCal host that is on the school's own domain or `libcal.com`, and only
after checking that the host resolves to public addresses.

### Verification and Planner

Verification checks every room offer: it must have a name and a room ID, be available, have a
booking link that is https on the school's domain or a known booking vendor, have a slot inside
the requested window when it has live times, and, when the seat count is known, fit the group.
Rejected offers are kept with their reasons. If no offer passes, the workflow fails with the first
reason.

For housing, Verification re-opens the office and portal links itself and drops any that do not
open.

The Planner turns the accepted offers into up to six room options, earliest opening first and then
the snuggest fit for the group. The first option is labeled **Earliest opening**. Each option has
a stable `option_id` derived from the room ID and its start time, so a button press always names
the same room and slot.

## The task pipeline

```text
Orchestrator ─TaskRequest─▶ Intake            ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Campus Researcher ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Campus Rooms      ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Verification      ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Planner           ─TaskResult─▶ Orchestrator ─▶ rooms carousel
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
- **One-time claims.** Each inbound chat message, each task result, each timeout, each pasted
  link, and each re-check press is claimed once in Postgres before it is acted on, so a
  redelivered message or a double press cannot start the same step twice.
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
| IPEDS cannot be read | The request fails with a retryable error rather than a guess |
| A specialist task is lost | The Orchestrator retries it once with a fresh request ID, then fails with a clear card |
| Campus Rooms does not answer a re-check | The rooms carousel comes back with a note, and nothing was handed over |
| A result arrives after the workflow moved on | It is ignored |

## State

| Table | Holds |
| --- | --- |
| `workflows` | One JSON state per workflow: service, stage, status, correlation, deadline, latest data, the chosen option |
| `workflow_events` | The append-only audit timeline written by Memory and Audit |
| `idempotency_keys` | One-time claims for chat messages, results, timeouts, pasted links, and re-checks |
| `api_cache` | Cached public lookups (IPEDS, Researcher results, LibCal pages) with an expiry |

Workflow statuses: `running` while a specialist works, `awaiting_link` when the Researcher needs a
pasted link, `awaiting_selection` when the rooms carousel is shown, `booked` after a successful
re-check, `answered` for a housing card, and `failed` with a reason.
