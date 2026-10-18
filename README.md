# ResourcePilot LA

Campus help for a student's first weeks, at any US school, run by a team of eight uAgents in one
ASI:One conversation. It works like Fetch's Welcome Week agents: a services menu of buttons and
short forms, with a card for every reply.

| Service | What you get | Source |
| --- | --- | --- |
| **Study rooms** | Real open library study rooms at your school, re-checked live, then a free booking link. You book with your own sign-in. | Live: your school's LibCal, found by the Campus Researcher |
| **Health & wellness** | Open meditation, reflection, and sensory rooms at your school, the week's health and wellness events with RSVP links, and the 988 crisis line on every card | Live: your school's LibCal and its public Localist events calendar |
| **Campus housing** | Every on-campus hall your school lists, with its photo, style, who it's for, features, and listed price, matched to what you need, plus IPEDS facts and the apply link | Live: the school's own housing pages plus IPEDS |
| **Emergency (demo)** | A demo-drill phone call that describes an emergency to your team's demo contact, never 911, then the call's summary and transcript | ElevenLabs voice agent over your own Twilio number |
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

- **Eight real uAgents.** Each has its own seed, address, port, process, profile README, Chat
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

Already have a `.env` from the 15-agent version? Keep it. The agents keep their seeds, so their
addresses and Agentverse profiles stay the same, and they keep the ports already in your `.env`.
`make verify` names the leftover settings for agents that no longer run; you may delete them.

Already have a `.env` without the Emergency Dispatcher? Add the ElevenLabs and demo-number settings
from [agents/emergency/README.md](agents/emergency/README.md), then run `make bootstrap-missing`.
It adds the Dispatcher's identity without changing any other seed. If port 8007 is already used by
another agent in your `.env` (older layouts used 8007 for Memory and Audit), it picks the next
free port.

Then connect the agents to Agentverse. Open every Inspector link that `make profiles` prints, and
connect each of the eight agents with **Mailbox**. The Agentverse Basic plan allows 8 Mailbox
agents, so all eight fit exactly. This is an account-bound step that you must do yourself in the
browser. See [docs/agentverse-deployment.md](docs/agentverse-deployment.md).

## The agents

| Key | Agent | Responsibility | Protocols | Port |
| --- | --- | --- | --- | --- |
| `orchestrator` | ResourcePilot Orchestrator | Runs the ASI:One chat, the services menu, cards, and the workflow | Chat, Tasks | 8000 |
| `intake` | ResourcePilot Intake | Turns a submitted form into a validated brief, keeping only the school domain | Chat, Tasks | 8001 |
| `researcher` | ResourcePilot Campus Researcher | Finds a school's room system, housing pages, and events feed from its email domain | Chat, Tasks | 8002 |
| `campus_rooms` | ResourcePilot Campus Rooms | Reads open study and wellness rooms live and re-checks one before handing over its link | Chat, Tasks | 8003 |
| `verification` | ResourcePilot Verification | Checks every room offer and re-opens housing links and event feeds before you see them | Chat, Tasks | 8004 |
| `planner` | ResourcePilot Planner | Ranks open rooms and matches campus housing to your criteria | Chat, Tasks | 8005 |
| `memory_audit` | ResourcePilot Memory and Audit | Keeps the append-only audit timeline and your recent bookings | Chat, Tasks | 8006 |
| `emergency` | ResourcePilot Emergency Dispatch | Places one demo-drill call to your team's demo contact, never 911, and reports the transcript | Chat, Tasks | 8007 |

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

Housing       Intake ─▶ Campus Researcher (reads each hall's page) ─▶ Verification (re-opens
              each hall and link) ─▶ Planner (matches your criteria) ─▶ halls carousel ─▶ hall

Emergency     Emergency Dispatcher (preview: checks the form) ─▶ review card
(demo)        "Call now" ─▶ Emergency Dispatcher places one call to the demo contact ─▶ call card
              "Check the call" ─▶ Emergency Dispatcher reads the call ─▶ summary and transcript
              "Cancel" ─▶ no call

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

For housing, the form asks who you are (first-year, transfer, continuing, graduate, or family),
the style you want, must-haves (A/C, kitchen, private bath, gender-inclusive, accessible), and an
optional yearly budget. The Researcher reads each hall from the school's own housing pages,
Verification re-opens every hall page and official link itself, and the Planner marks each
criterion as a match, not listed, or ruled out by the hall's own page. Ruled-out halls are hidden
behind **Show all**; halls whose pages don't say come after the matches. For wellness, it re-fetches the school's
events feed itself and drops any event the feed no longer lists.

The Campus Researcher reads only public pages over https, on your school's domain or a known
booking, housing, or events vendor (Localist). It checks that every address it connects to is
public, vets every redirect, and stops after a small fetch and time budget. Page text is treated
as evidence, never as instructions. From an events feed it keeps only a capped title, time,
location, and link. Campus Rooms reads only a LibCal host on the school's own domain or `libcal.com`.
See [docs/trust-model.md](docs/trust-model.md).

The emergency call is a demo drill. The Emergency Dispatcher dials only the number in
`EMERGENCY_DEMO_TO_NUMBER`; nothing in chat, the form, or a task can choose it. It refuses 911,
112, 999, 988, N11 codes, listed crisis hotlines, and short numbers. The voice always opens with
"this is a ResourcePilot demo drill, not a real emergency". Only the **Call now** button can place
the call, at most once per workflow, and it is never retried. Cards show only the number's last
four digits, and every emergency card says to call 911 in a real emergency. Only the Emergency
Dispatcher talks to ElevenLabs, and errors never include the API key or the full number.

## Useful commands

```bash
make start              # Postgres + 8 independently running uAgents
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
offline fakes, and the ElevenLabs API is mocked, so no test touches the network or places a call.
Set `TEST_DATABASE_URL` to also run the store contract and the end-to-end network tests on real
Postgres. CI does this.

## Documentation

- [docs/demo-script.md](docs/demo-script.md): a three-minute demo that shows every judged feature
- [docs/architecture.md](docs/architecture.md): messages, state, timeouts, the re-check, and
  recovery
- [docs/trust-model.md](docs/trust-model.md): room-offer checks, housing link re-checks, research
  bounds, the demo call's rules, and their limits
- [docs/agentverse-deployment.md](docs/agentverse-deployment.md): Mailbox for 8 agents and
  persistent hosting
- [docs/judging-checklist.md](docs/judging-checklist.md): evidence to capture before submitting
- [docs/official-links.md](docs/official-links.md): the official Fetch.ai sources this follows
- [AGENTS.md](AGENTS.md): invariants for AI coding assistants

## What this does not claim

ResourcePilot takes no payments of any kind: there is no payment rail, no Stripe integration, no
FET, and no escrow. It does not reserve study or wellness rooms, RSVP to events, or apply for,
reserve, or pay for housing. It links you to your school's own site, where you sign in yourself.
Hall details come from each school's own pages and only as far as those pages say: schools whose
halls are not linked from the housing site, or are on a separate site, fall back to the housing
facts and links, and prices appear only when a hall's page lists one.
It never asks about your health, books health or counseling appointments, or gives health advice.
The 988 line on the wellness card is static text, not a referral service.

The emergency feature never calls 911, campus police, or any emergency service. It is a demo drill
that phones one teammate's phone, set in `EMERGENCY_DEMO_TO_NUMBER`. Live calls were not part of
the automated tests, which mock ElevenLabs; a live call needs your own ElevenLabs and Twilio setup,
and both bill calls to your accounts. In a real emergency, call 911.

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
