# ResourcePilot LA

One ASI:One conversation books campus resources across several independent seller agents. It holds
every item first and takes payment only after every seller has confirmed. Sellers are paid only
after every booking commits.

> "I need a DSLR, a podcast room, and 20 color posters Friday from 3-6 PM, under $35."

Today that is three websites, three accounts, and three separate checkouts that can half-succeed:
the room gets booked, the camera is gone, and the money for the room is already spent.
ResourcePilot turns it into one request and one payment, with all-or-nothing completion across
sellers that do not know each other.

- **Thirteen real uAgents.** There are eight platform agents and five independent demo seller
  agents. Each has its own seed, address, port, process, profile README, and Chat Protocol
  manifest.
- **Live multi-seller negotiation.** Sellers answer real quote, hold, and commit messages over a
  typed `ResourceHold` protocol. Nothing is looked up from a shared table.
- **A trust gate before money.** Seller Verification rejects unverified, malformed, unreliable, or
  policy-violating offers before you see a bundle, and shows you why.
- **Hold, then pay, then commit.** Escrow runs over the official Agent Payment Protocol, with a Mock
  Stripe rail (no money moves) and a Fetch stable-testnet FET rail (verified on chain).
- **Recovery is part of the product.** If a seller goes silent, the Planner reroutes around it. A
  failed booking keeps your payment in escrow for one-click retry or refund, and refunds
  automatically if you wait.

This repository uses public Fetch.ai packages and documentation only. The sellers are demo
businesses with illustrative catalogs, not real shops.

## Start here

Prerequisites: Docker Desktop, Python 3.11–3.13, and
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
make setup
make start
make profiles
```

`make setup` installs locked dependencies and creates an ignored `.env` containing 13 random private
seeds. It derives every address and seller wallet and writes `.profiles.json`. It never prints a
seed and asks before replacing an existing `.env`, because new seeds mean new agent identities.

Then connect the agents to Agentverse. Open every Inspector link that `make profiles` prints, and
connect each agent with **Mailbox**: eight platform agents and five sellers. This is an
account-bound step that you must do yourself in the browser. See
[docs/agentverse-deployment.md](docs/agentverse-deployment.md).

## The agents

| Agent | Responsibility | Protocols | Port |
| --- | --- | --- | --- |
| Orchestrator | ASI:One entrypoint, workflow owner, cards, escrow payment | Chat, Tasks, Payment (`seller`) | 8000 |
| Intake | Parses a sentence or form into a validated brief | Chat, Tasks | 8001 |
| Seller Discovery | Fans out quote requests to sellers and collects real quotes | Chat, Tasks, ResourceHold (`buyer`) | 8002 |
| Seller Verification | Trust gate: identity, protocol, track record, policy | Chat, Tasks | 8003 |
| Logistics Analysis | Lead times and walking distance from your anchor | Chat, Tasks | 8004 |
| Bundle Planner | Budget-safe bundles across sellers; reroutes around failures | Chat, Tasks | 8005 |
| Checkout Coordinator | All-or-nothing hold and commit rounds across sellers | Chat, Tasks, ResourceHold (`buyer`) | 8006 |
| Memory and Audit | Append-only workflow events, preferences, seller track records | Chat, Tasks | 8007 |

| Demo seller | Sells | Purpose | Port |
| --- | --- | --- | --- |
| Westwood Print Lab | Posters | Verified print shop | 8100 |
| Gayley Copy Center | Posters | Verified alternative for rerouting | 8101 |
| Flash Prints 4U | Posters | Cheapest, but **unverified**; Verification rejects it | 8102 |
| Campus Gear Share | Cameras, microphones, tripods | Verified; DSLR requires training | 8103 |
| Hilltop Studio Rooms | Podcast and study rooms | Verified; one podcast studio | 8104 |

The Orchestrator delegates every step with `ctx.send` and correlated `TaskRequest`/`TaskResult`
messages: workflow, request, requester, and session IDs, plus deadlines and duplicate protection.
Postgres is the shared durable store for workflows, rounds, holds, payments, the escrow ledger,
audit events, and seller track records.

## How one booking flows

```text
You ──chat──▶ Orchestrator ──▶ Intake ──▶ Seller Discovery ══QuoteRequest══▶ 5 sellers
                                              ◀═════Quote═════
            ◀── carousel ── Bundle Planner ◀── Logistics ◀── Seller Verification
"Hold everything" ──▶ Checkout Coordinator ══HoldRequest══▶ each seller in the bundle
            ◀── review ──  (all confirmed, or every hold is released)
"Pay"  ◀── RequestPayment ── Orchestrator (escrow)
CommitPayment ──▶ verify ─▶ reserve ─▶ Checkout Coordinator ══CommitHold══▶ each seller
            ◀── receipt ── seller payouts recorded in the escrow ledger
```

Details are in [docs/architecture.md](docs/architecture.md).

## Money: escrow and a visible booking fee

You pay each seller's own quoted price plus a booking fee: 3% of the subtotal plus 30 cents,
set by `PLATFORM_FEE_BPS` and `PLATFORM_FEE_FIXED_CENTS`. The money goes to the Orchestrator's
escrow. Each seller receives exactly what it quoted, and ResourcePilot keeps the fee, only after
every booking commits. If anything fails, no seller is paid.

- **Mock Stripe** calls no Stripe API, makes no chain transaction, and moves no money. `make
  stripe-mock` runs a demo buyer agent that clicks through the cards and commits a `STRIPE_MOCK:`
  payment.
- **Pay test FET** uses faucet-issued tokens on Fetch stable-testnet. The Orchestrator checks the
  on-chain transfer with cosmpy before anything is booked: sender, escrow recipient, minimum
  amount, denom, network, and transaction replay. Seller payouts are recorded in the ledger. They
  are sent as testnet transfers only when `FET_PAYOUTS_ENABLED=true`.

FET mainnet and real-money payments are permanently unsupported. See
[docs/payments.md](docs/payments.md).

## Trust: why the Orchestrator may send you to a seller

Sellers come from a curated registry, so the registry is where identity lives. Every quote then
passes four checks:

1. **Identity.** The quote's authenticated sender must match the registry address of a verified
   listing.
2. **Protocol conformance.** Version, currency, hold time, listed categories, and arithmetic must
   all check out.
3. **Track record.** Sellers that fail too often are dropped, and new sellers have a capped line
   value.
4. **Policy.** Items must be available and meet training requirements.

Optional Almanac discovery (`ALMANAC_DISCOVERY=true`) also asks agents that publish the
`ResourceHold` seller protocol. Unlisted sellers are always rejected at the identity check. See
[docs/trust-model.md](docs/trust-model.md).

## Useful commands

```bash
make start          # Postgres + 13 independently running uAgents
make logs           # follow all service logs
make profiles       # addresses and Inspector/Profile URLs (never seeds)
make verify         # check .env identities and the Compose file
make stripe-mock    # NO-MONEY demo buyer that books one bundle end to end
make check          # format, lint, type-check, and tests
make build          # build the Docker image
make stop
```

The test suite drives the real agent handlers in process: every role and seller is loaded as its own
module with its own address. Set `TEST_DATABASE_URL` to also run the store contract and end-to-end
checkout on real Postgres. CI does this.

## Documentation

- [docs/demo-script.md](docs/demo-script.md): a three-minute demo that shows every judged feature
- [docs/architecture.md](docs/architecture.md): messages, state, rounds, timeouts, and recovery
- [docs/trust-model.md](docs/trust-model.md): seller registry, vetting layers, and their limits
- [docs/payments.md](docs/payments.md): escrow, rails, verification, ledger, and refunds
- [docs/agentverse-deployment.md](docs/agentverse-deployment.md): Mailbox for 13 agents and
  persistent hosting
- [docs/judging-checklist.md](docs/judging-checklist.md): evidence to capture before submitting
- [docs/official-links.md](docs/official-links.md): the official Fetch.ai sources this follows
- [AGENTS.md](AGENTS.md): invariants for AI coding assistants

## What this does not claim

The Stripe rail is a local mock, not a Stripe integration or proof of payment. The sellers, their
catalogs, and their registry "verified" flags are demo data. This project does not use MCP,
Google's A2A protocol, UCP, Skyfire, or AEVs. Almanac discovery is optional and off by default, and
it has been tested only against a simulated Almanac. Profile links, chat links, and testnet
transactions exist only after you run the manual steps above.

## Credits

Licensed under the [MIT License](LICENSE).
