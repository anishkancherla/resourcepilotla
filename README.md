# ResourcePilot LA

Campus help for a student's first weeks, at any US school, run by a team of seven uAgents in one
ASI:One conversation. It works like Fetch's Welcome Week agents: a services menu of buttons and
short forms, with a card for every reply.

| Service | What you get | Source |
| --- | --- | --- |
| **Study rooms** | Real open library study rooms at your school, re-checked live, then a free booking link. You book with your own sign-in. | Live: your school's LibCal, found by the Campus Researcher |
| **Health & wellness** | Open meditation, reflection, and sensory rooms at your school, the week's health and wellness events with RSVP links, and the 988 crisis line on every card | Live: your school's LibCal and its public Localist events calendar |
| **Campus housing** | Dorm beds, typical room and board, meal plans, your housing office, and its application portal, with each link re-checked | Live: IPEDS plus the school's own housing site |
| **My bookings** | Your recent re-checked rooms and their booking links | ResourcePilot's workflow records |

Study rooms work for **any US school**, not only one campus. You enter your school email. Intake
keeps only the domain, and the Campus Researcher matches it to the school in IPEDS. It then finds
the library's room-booking system:

- a hand-checked list;
- a `<school>.libcal.com` probe;
- a bounded crawl of the library site, which finds custom-domain systems such as Purdue's
  `calendar.lib.purdue.edu`.

If it finds nothing, it asks you for the link instead of guessing.

Rooms are always free. ResourcePilot re-checks that the room is still open, then hands you the
school's booking link: **you book it with your own sign-in**. ResourcePilot never reserves a room.

- **Seven real uAgents.** Each has its own seed, address, port, process, profile README, Chat
  Protocol manifest, and status card, and each does one job in the workflow.
- **Real delegation.** The Orchestrator hands every step to another agent with `ctx.send` and a
  correlated `TaskRequest`, and accepts only the matching `TaskResult`.
- **Live data, checked twice.** Verification checks every room offer before you see it and
  re-opens every official housing link itself. Campus Rooms reads the room again, uncached, right
  before you get the link.
- **Recovery is part of the product.** If a room is taken during the re-check, the Planner
  reroutes to the other open rooms. A lost task is retried once. If LibCal's live times fail, you
  get the school's booking link, never invented rooms.

This repository uses public Fetch.ai packages and documentation only.

## Start here

Prerequisites: Docker Desktop, Python 3.11–3.13, and
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
make setup
make start
make profiles
```

`make setup` installs locked dependencies and creates an ignored `.env` containing one random
private seed per agent. It derives every address and writes `.profiles.json`. It never prints a
seed and asks before replacing an existing `.env`, because new seeds mean new agent identities.

Already have a `.env` from the 15-agent version? Keep it. The seven agents keep their seeds, so
their addresses and Agentverse profiles stay the same, and they keep the ports already in your
`.env`. `make verify` names the leftover settings for agents that no longer run; you may delete
them.

Then connect the agents to Agentverse. Open every Inspector link that `make profiles` prints, and
connect each of the seven agents with **Mailbox**. The Agentverse Basic plan allows 8 Mailbox
agents, so all seven fit. This is an account-bound step that you must do yourself in the browser.
See [docs/agentverse-deployment.md](docs/agentverse-deployment.md).

## The agents

| Key | Agent | Responsibility | Protocols | Port |
| --- | --- | --- | --- | --- |
| `orchestrator` | ResourcePilot Orchestrator | Runs the ASI:One chat, the services menu, cards, and the workflow | Chat, Tasks | 8000 |
| `intake` | ResourcePilot Intake | Turns a submitted form into a validated brief, keeping only the school domain | Chat, Tasks | 8001 |
| `researcher` | ResourcePilot Campus Researcher | Finds a school's room system, housing pages, and events feed from its email domain | Chat, Tasks | 8002 |
| `campus_rooms` | ResourcePilot Campus Rooms | Reads open study and wellness rooms live and re-checks one before handing over its link | Chat, Tasks | 8003 |
| `verification` | ResourcePilot Verification | Checks every room offer and re-opens housing links and event feeds before you see them | Chat, Tasks | 8004 |
| `planner` | ResourcePilot Planner | Ranks open rooms and reroutes when a room is taken | Chat, Tasks | 8005 |
| `memory_audit` | ResourcePilot Memory and Audit | Keeps the append-only audit timeline and your recent bookings | Chat, Tasks | 8006 |

"Chat" is the Agent Chat Protocol; "Tasks" is ResourcePilot's own `ResourcePilotTasks` protocol
(`TaskRequest`, `TaskResult`, `AuditEvent`). Every agent publishes both manifests. Each one's
profile chat answers with a status card that names its role and points to the Orchestrator.

The Orchestrator delegates every step with `ctx.send` and correlated `TaskRequest`/`TaskResult`
messages: workflow, request, requester, and session IDs, plus deadlines and duplicate protection.
Postgres is the shared durable store for workflows, the audit timeline, one-time claims, and the
cache of public lookups.

## How each service flows

```text
Study rooms   Intake ─▶ Campus Researcher ─▶ Campus Rooms (live LibCal) ─▶ Verification
              ─▶ Planner ─▶ rooms carousel
              "View room" ─▶ "Re-check this room" ─▶ Campus Rooms re-reads that slot live
                 still open ─▶ your school's booking link
                 taken      ─▶ Planner again, without that room ─▶ rooms carousel
              No booking system found ─▶ paste-link card ─▶ Campus Researcher checks the link

Wellness      Intake ─▶ Campus Researcher (room system + events feed) ─▶ Campus Rooms
              (wellness rooms) ─▶ Verification (re-fetches the feed) ─▶ Planner
              ─▶ wellness card: 988 first, open rooms, this week's events
              "View room" ─▶ the same live re-check and booking link as study rooms
              No room system: Intake ─▶ Campus Researcher ─▶ Verification ─▶ wellness card

Housing       Intake ─▶ Campus Researcher ─▶ Verification (re-opens each link) ─▶ housing card

My bookings   Orchestrator ─▶ your recent re-checked rooms
```

Every arrow between agents is a `TaskRequest` sent with `ctx.send` and answered by a `TaskResult`.
Requests start only from a form; free text gets the services menu. Details are in
[docs/architecture.md](docs/architecture.md).

## Trust: why the Orchestrator may send you to a link

Verification checks every room offer before you see it. An offer is dropped, with the reason
recorded, unless all of these hold:

1. The room is available, and the offer has a room name and a stable room ID.
2. The booking link is https on your school's domain or its known booking vendor.
3. When the offer has live times, its slot falls inside the window you asked for.
4. When the room's seat count is known, it fits your group.

For housing, Verification re-opens the housing office and application portal links itself and
drops any that no longer open on the school's site. For wellness, it re-fetches the school's
events feed itself and drops any event the feed no longer lists.

The Campus Researcher reads only public pages over https, on your school's domain or a known
booking, housing, or events vendor (Localist). It checks that every address it connects to is
public, vets every redirect, and stops after a small fetch and time budget. Page text is treated
as evidence, never as instructions. From an events feed it keeps only a capped title, time,
location, and link. Campus Rooms reads only a LibCal host on the school's own domain or `libcal.com`.
See [docs/trust-model.md](docs/trust-model.md).

## Useful commands

```bash
make start              # Postgres + 7 independently running uAgents
make logs               # follow all service logs
make profiles           # addresses and Inspector/Profile URLs (never seeds)
make verify             # check .env identities and the Compose file
make check              # format, lint, type-check, and tests
make build              # build the Docker image
make stop
```

`uv run python scripts/print_cards.py` prints every card payload, validated exactly as the agents
send it. Pass a school domain, such as `purdue.edu`, to also run the real Researcher and Campus
Rooms logic against public pages, including the wellness card's rooms and events. It needs no keys
and never books anything.

The test suite drives the real agent handlers in process: every role is loaded as its own module
with its own address. Every live source (IPEDS, LibCal, Localist, and school websites) is served by
offline fakes, so no test touches the network. Set `TEST_DATABASE_URL` to also run the store contract and
the end-to-end network tests on real Postgres. CI does this.

## Documentation

- [docs/demo-script.md](docs/demo-script.md): a three-minute demo that shows every judged feature
- [docs/architecture.md](docs/architecture.md): messages, state, timeouts, the re-check, and
  recovery
- [docs/trust-model.md](docs/trust-model.md): room-offer checks, housing link re-checks, research
  bounds, and their limits
- [docs/agentverse-deployment.md](docs/agentverse-deployment.md): Mailbox for 7 agents and
  persistent hosting
- [docs/judging-checklist.md](docs/judging-checklist.md): evidence to capture before submitting
- [docs/official-links.md](docs/official-links.md): the official Fetch.ai sources this follows
- [AGENTS.md](AGENTS.md): invariants for AI coding assistants

## What this does not claim

ResourcePilot takes no payments of any kind: there is no payment rail, no Stripe integration, no
FET, and no escrow. It does not reserve study or wellness rooms, RSVP to events, or apply for,
reserve, or pay for housing. It links you to your school's own site, where you sign in yourself.
It never asks about your health, books health or counseling appointments, or gives health advice.
The 988 line on the wellness card is static text, not a referral service.

Wellness rooms are found only when a school lists them in LibCal under names such as meditation,
reflection, prayer, lactation, or sensory, so many schools will show none. Wellness events come
only from a public Localist calendar on `events.<domain>`, `calendar.<domain>`, or
`<school>.localist.com`. Other calendars are not read, and the keyword filter can include events a
school tags as wellness, such as a farmers market.

Campus Rooms reads open times from the endpoint that LibCal's public booking page calls. That
endpoint is not a documented API and may change. When it fails, you get the school's booking link,
never invented rooms. For other systems (EMS, 25Live, Skedda, Robin, or a plain booking page)
availability is not public, so you get only the link. IPEDS facts are the 2022 survey year; check
current prices with the school.

This project does not use MCP, Google's A2A protocol, UCP, Skyfire, or AEVs. Profile links and
chat links exist only after you run the manual steps above.

## Credits

School data comes from IPEDS via the Urban Institute Education Data API. Licensed under the
[MIT License](LICENSE).
