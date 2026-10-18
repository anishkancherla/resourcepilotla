# ResourcePilot LA

Campus help for a student's first weeks, run by a team of uAgents in one ASI:One conversation.
It works like Fetch's Welcome Week agents: a services menu of buttons and short forms, with a
card for every reply.

| Service | What you get | Source |
| --- | --- | --- |
| **Study rooms** | Real open library study rooms at your school, re-checked live before you book | Live: your school's LibCal, found by the Campus Researcher |
| **Health & wellness** | Open meditation, reflection, and sensory rooms at your school, the week's health and wellness events with RSVP links, and the 988 crisis line on every card | Live: your school's LibCal and its public Localist events calendar |
| **Campus housing** | Dorm beds, typical room and board, meal plans, your housing office, and its application portal | Live: IPEDS plus the school's own housing site |
| **Project bundle** | Camera, podcast studio, and posters from several sellers, held all at once before you pay | Demo shops near UCLA (Mock Stripe or test FET only) |
| **My bookings** | Your recent bookings and room links | ResourcePilot's audit trail |

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

The project bundle keeps the original ResourcePilot idea: one request and one payment across
sellers that do not know each other, with all-or-nothing completion. You can add a free study room
at your school to a bundle; it rides along outside escrow.

- **Fifteen real uAgents.** Nine platform agents, five demo seller agents, and one live seller,
  Campus Rooms. Each has its own seed, address, port, process, profile README, and Chat Protocol
  manifest.
- **Live multi-seller negotiation.** Sellers answer real quote, hold, and commit messages over a
  typed `ResourceHold` protocol (1.1.0). Nothing is looked up from a shared table.
- **A trust gate before money.** Seller Verification rejects unverified, malformed, unreliable, or
  policy-violating offers before you see a bundle, and shows you why. It also re-opens every
  official housing link itself.
- **Hold, then pay, then commit.** Escrow runs over the official Agent Payment Protocol, with a Mock
  Stripe rail (no money moves) and a Fetch stable-testnet FET rail (verified on chain).
- **Recovery is part of the product.** If a seller goes silent, the Planner reroutes around it. If
  a room is taken during the re-check, you see the other open rooms. A failed booking keeps your
  payment in escrow for one-click retry or refund, and refunds automatically if you wait.

This repository uses public Fetch.ai packages and documentation only. The five demo sellers are
demo businesses with illustrative catalogs, not real shops.

## Start here

Prerequisites: Docker Desktop, Python 3.11–3.13, and
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
make setup
make start
make profiles
```

`make setup` installs locked dependencies and creates an ignored `.env` containing 15 random private
seeds. It derives every address and seller wallet and writes `.profiles.json`. It never prints a
seed and asks before replacing an existing `.env`, because new seeds mean new agent identities.

Already have a `.env` from the 13-agent version? Run `make bootstrap-missing` instead. It asks
first, then adds identities only for the Campus Researcher and Campus Rooms. Every existing seed
and address stays as it is.

Then connect the agents to Agentverse. Open every Inspector link that `make profiles` prints, and
connect each agent with **Mailbox**: nine platform agents and six sellers. This is an
account-bound step that you must do yourself in the browser. See
[docs/agentverse-deployment.md](docs/agentverse-deployment.md).

## The agents

| Agent | Responsibility | Protocols | Port |
| --- | --- | --- | --- |
| Orchestrator | ASI:One entrypoint, services menu, workflow owner, cards, escrow payment | Chat, Tasks, Payment (`seller`) | 8000 |
| Intake | Turns a submitted form into a validated brief; keeps only the school domain | Chat, Tasks | 8001 |
| Seller Discovery | Fans out quote requests to sellers and collects real quotes | Chat, Tasks, ResourceHold (`buyer`) | 8002 |
| Seller Verification | Trust gate (identity, protocol, track record, policy); re-checks housing links and event feeds | Chat, Tasks | 8003 |
| Logistics Analysis | Lead times and walking distance from your anchor | Chat, Tasks | 8004 |
| Bundle Planner | Budget-safe bundles across sellers; ranks open rooms; reroutes around failures | Chat, Tasks | 8005 |
| Checkout Coordinator | All-or-nothing hold and commit rounds across sellers | Chat, Tasks, ResourceHold (`buyer`) | 8006 |
| Memory and Audit | Append-only workflow events, preferences, seller track records | Chat, Tasks | 8007 |
| Campus Researcher | School from the email domain; its room-booking system, housing pages, and events feed | Chat, Tasks | 8008 |

| Seller | Sells | Purpose | Port |
| --- | --- | --- | --- |
| Westwood Print Lab | Posters | Verified demo print shop | 8100 |
| Gayley Copy Center | Posters | Verified demo alternative for rerouting | 8101 |
| Flash Prints 4U | Posters | Cheapest, but **unverified**; Verification rejects it | 8102 |
| Campus Gear Share | Cameras, microphones, tripods | Verified demo shop; DSLR requires training | 8103 |
| Hilltop Studio Rooms | Podcast studio | Verified demo shop; one studio | 8104 |
| Campus Rooms | Study and wellness rooms at your school | **Live**: open LibCal rooms as $0 booking handoffs | 8105 |

The Orchestrator delegates every step with `ctx.send` and correlated `TaskRequest`/`TaskResult`
messages: workflow, request, requester, and session IDs, plus deadlines and duplicate protection.
Each service has its own chain of specialists. Postgres is the shared durable store for workflows,
rounds, holds, payments, the escrow ledger, audit events, seller track records, and the cache of
public lookups.

## How each service flows

```text
Study rooms   Intake ─▶ Campus Researcher ─▶ Seller Discovery ══▶ Campus Rooms
              ─▶ Seller Verification ─▶ Logistics ─▶ Bundle Planner ─▶ rooms carousel
              "Re-check this room" ══HoldRequest══▶ Campus Rooms (live re-check)
              "Get booking link"   ══CommitHold═══▶ Campus Rooms ─▶ your school's booking link

Wellness      Intake ─▶ Campus Researcher (room system + events feed) ─▶ Seller Discovery
              ══▶ Campus Rooms (wellness rooms) ─▶ Verification (re-fetches the feed) ─▶ Logistics
              ─▶ Bundle Planner ─▶ wellness card: 988 first, open rooms, this week's events
              No room system: Intake ─▶ Campus Researcher ─▶ Verification ─▶ wellness card

Housing       Intake ─▶ Campus Researcher ─▶ Seller Verification (re-opens each link) ─▶ card

Bundle        Intake ─▶ [Campus Researcher, if you add a free room] ─▶ Seller Discovery
              ══QuoteRequest══▶ sellers ─▶ Verification ─▶ Logistics ─▶ Planner ─▶ carousel
              "Hold everything" ─▶ Checkout Coordinator ══HoldRequest══▶ every seller
              "Pay" ◀── RequestPayment ── Orchestrator (escrow)
              CommitPayment ─▶ verify ─▶ reserve ─▶ CommitHold to every seller ─▶ receipt
```

Details are in [docs/architecture.md](docs/architecture.md).

## Money: escrow, a visible booking fee, and free rooms

In a bundle, you pay each demo seller's own quoted price plus a booking fee: 3% of the subtotal
plus 30 cents, set by `PLATFORM_FEE_BPS` and `PLATFORM_FEE_FIXED_CENTS`. The money goes to the
Orchestrator's escrow. Each seller receives exactly what it quoted, and ResourcePilot keeps the
fee, only after every booking commits. If anything fails, no seller is paid.

- **Mock Stripe** calls no Stripe API, makes no chain transaction, and moves no money. `make
  stripe-mock` runs a demo buyer agent that clicks through the cards and commits a `STRIPE_MOCK:`
  payment.
- **Pay test FET** uses faucet-issued tokens on Fetch stable-testnet. The Orchestrator checks the
  on-chain transfer with cosmpy before anything is booked: sender, escrow recipient, minimum
  amount, denom, network, and transaction replay. Seller payouts are recorded in the ledger. They
  are sent as testnet transfers only when `FET_PAYOUTS_ENABLED=true`.
- **Study rooms are $0** and are never part of escrow or the fee. A rooms-only request has no
  payment step at all: after the live re-check, you get the booking link.

FET mainnet and real-money payments are permanently unsupported. See
[docs/payments.md](docs/payments.md).

## Trust: why the Orchestrator may send you to a seller or a link

Sellers come from a curated registry, so the registry is where identity lives. Every quote then
passes four checks:

1. **Identity.** The quote's authenticated sender must match the registry address of a verified
   listing.
2. **Protocol conformance.** Version, currency, hold time, listed categories, and arithmetic must
   all check out.
3. **Track record.** Sellers that fail too often are dropped, and new sellers have a capped line
   value.
4. **Policy.** Items must be available and meet training requirements. Campus Rooms offers must
   be $0 handoffs whose link is on your school's domain or its booking vendor.

The Campus Researcher reads only public pages over https, on your school's domain or a known
booking, housing, or events vendor (Localist). It checks that every address it connects to is
public, vets every redirect, and stops after a small fetch and time budget. Page text is treated
as evidence, never as instructions. From an events feed it keeps only a capped title, time,
location, and link. See [docs/trust-model.md](docs/trust-model.md).

Optional Almanac discovery (`ALMANAC_DISCOVERY=true`) also asks agents that publish the
`ResourceHold` seller protocol. Unlisted sellers are always rejected at the identity check.

## Useful commands

```bash
make start              # Postgres + 15 independently running uAgents
make logs               # follow all service logs
make profiles           # addresses and Inspector/Profile URLs (never seeds)
make verify             # check .env identities and the Compose file
make bootstrap-missing  # add identities for new agents without changing existing ones
make stripe-mock        # NO-MONEY demo buyer that books one bundle end to end
make check              # format, lint, type-check, and tests
make build              # build the Docker image
make stop
```

`uv run python scripts/print_cards.py purdue.edu` prints every card payload, validated exactly as
the agents send it. With school domains, it runs the real Researcher and Campus Rooms quote
against public pages, including the wellness card's rooms and events.

The test suite drives the real agent handlers in process: every role and seller is loaded as its own
module with its own address. Every live source (IPEDS, LibCal, Localist, and school websites) is
served by offline fakes, so no test touches the network. Set `TEST_DATABASE_URL` to also run the
store contract and end-to-end checkout on real Postgres. CI does this.

## Documentation

- [docs/demo-script.md](docs/demo-script.md): a three-minute demo that shows every judged feature
- [docs/architecture.md](docs/architecture.md): messages, state, rounds, timeouts, and recovery
- [docs/trust-model.md](docs/trust-model.md): seller registry, vetting layers, research bounds,
  and their limits
- [docs/payments.md](docs/payments.md): escrow, rails, verification, ledger, refunds, and free
  room handoffs
- [docs/agentverse-deployment.md](docs/agentverse-deployment.md): Mailbox for 15 agents and
  persistent hosting
- [docs/judging-checklist.md](docs/judging-checklist.md): evidence to capture before submitting
- [docs/official-links.md](docs/official-links.md): the official Fetch.ai sources this follows
- [AGENTS.md](AGENTS.md): invariants for AI coding assistants

## What this does not claim

ResourcePilot does not reserve study or wellness rooms, RSVP to events, or apply for or pay for
housing. It links you to your school's own site, where you sign in yourself. It never asks about
your health, books health or counseling appointments, or gives health advice. The 988 line on the
wellness card is static text, not a referral service.

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

The Stripe rail is a local mock, not a Stripe integration or proof of payment. The five demo
sellers, their catalogs, and their registry "verified" flags are demo data. This project does not
use MCP, Google's A2A protocol, UCP, Skyfire, or AEVs. Almanac discovery is optional and off by
default, and it has been tested only against a simulated Almanac. Profile links, chat links, and
testnet transactions exist only after you run the manual steps above.

## Credits

School data comes from IPEDS via the Urban Institute Education Data API. Licensed under the
[MIT License](LICENSE).
