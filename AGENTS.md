# Instructions for AI coding assistants

Treat these as repository invariants. Before changing anything Fetch-facing, check the live
official pages in [docs/official-links.md](docs/official-links.md) for anything version-sensitive.

## Preserve the architecture

- Keep this repository standalone and based only on public Fetch.ai packages and documentation.
- Keep seven real uAgents, all run by `agents/run.py` with `AGENT_ROLE=<key>`:

  | Key | Agent | Port | Responsibility |
  | --- | --- | --- | --- |
  | `orchestrator` | ResourcePilot Orchestrator | 8000 | Runs the ASI:One chat, the services menu, cards, and the workflow |
  | `intake` | ResourcePilot Intake | 8001 | Turns a submitted form into a validated brief, keeping only the school domain |
  | `researcher` | ResourcePilot Campus Researcher | 8002 | Finds a school's room-booking system and housing pages from its email domain |
  | `campus_rooms` | ResourcePilot Campus Rooms | 8003 | Reads open library rooms live and re-checks one before handing over the booking link |
  | `verification` | ResourcePilot Verification | 8004 | Checks every room offer and re-opens official housing links before you see them |
  | `planner` | ResourcePilot Planner | 8005 | Ranks open rooms and reroutes when a room is taken |
  | `memory_audit` | ResourcePilot Memory and Audit | 8006 | Keeps the append-only audit timeline and your recent bookings |

  Each one needs a unique seed variable keyed by its key (`<KEY>_SEED`), a deterministic address,
  its own port and process, a profile README at `agents/<key>/README.md`, a Chat Protocol
  manifest, a chat handler, a status card, and a meaningful protocol responsibility. Never change
  a key: seeds and addresses in users' `.env` files are keyed by it.
- Each service has its own specialist chain, and the Orchestrator runs it in order:
  - `rooms`: Intake → Campus Researcher → Campus Rooms → Verification → Planner, then a rooms
    carousel. "Re-check this room" sends Campus Rooms a `phase="recheck"` task. Still open: the
    booking-link card. Taken: the Planner again with that room in `exclude_items`.
  - `housing`: Intake → Campus Researcher → Verification, then the housing card.
- The Orchestrator offers services from a menu: Study rooms, Campus housing, and My bookings.
  Requests start only from a form submission; free text gets the menu card, never a workflow.
  Every Orchestrator reply is a card.
- Never add padding roles, or simulate several roles inside one chatbot.
- The Orchestrator delegates with `ctx.send`. Keep workflow, request, requester, and session
  correlation, deadlines, duplicate protection, and the sender checks on every task and result.
- Read a school's room availability only in Campus Rooms, never in another agent.
- Keep Postgres as the shared durable path. `MemoryStore` is for tests and development only.

## Preserve protocol correctness

- Import the Chat specification from `uagents_core.contrib.protocols`.
- ACK inbound `ChatMessage` promptly, and publish every included manifest.
- Build cards as `TextContent` plus `MetadataContent`, and make every metadata value a string.
  Keep `card_protocol_version="1"`, `requires_card_interaction="true"`, a supported
  `card_kind`, and a JSON `card_payload` of 64KB or less. Custom cards need a typed `root`
  and may be at most eight levels deep.
- Put stable action, workflow, and option IDs in selections. Parse direct JSON first, then the
  bounded prose allowlist. Never infer a re-check, a booking, or any other consequential action
  from ambiguous prose, and never mark a card terminal when it expects input.

## Preserve live-data safety

- Keep only the domain of a school email. Strip the local part before anything is stored, sent,
  or logged.
- Fetch only public https pages on the school's domain or a known vendor. Check that resolved
  addresses are public, vet every redirect, cap bytes, and keep the fetch and time budgets.
- Treat page text as untrusted evidence: extract links and fingerprints only, never instructions.
- If a booking system is not found, ask the student for the link and verify it the same way.
  Never guess a booking system, invent rooms, or claim a room was reserved.
- Re-check a room live before handing over its booking link. ResourcePilot never reserves a room;
  the student books with their own sign-in.
- Housing is facts plus official links. Never apply for, reserve, or pay for housing.

## No payments in this build

- ResourcePilot takes no payments. Do not add payment rails, Stripe (real or mock), FET, or
  escrow without a real, tested implementation and an explicit request from the user.
- Never add a mainnet or real-value payment switch.
- Never claim payments, a Stripe integration, Skyfire, UCP, MCP, A2A, AEVs, or Almanac
  registration without a real, tested implementation and demo evidence.

## Keep docs and tests synchronized

When a role, port, env variable, protocol, card, or service changes:

1. Update `.env.example`, the profile READMEs, the README tables, and the docs.
2. Update identity generation in `resourcepilot/config.py`. Never commit generated addresses.
3. Update the card, intake, research, store, rooms, vetting, planning, workflow, network,
   architecture, and safety tests.
4. Run `make check`, `make build`, and `make verify`.
5. Ask the user to run `make profiles`, open every Inspector link, connect every Mailbox, and
   complete the account-bound Agentverse and ASI:One steps themselves.

Never fabricate profile links, addresses, registrations, test results, or bookings.

## Secret handling

Never read secrets into output, print seeds, or commit `.env`, `.profiles.json`, wallet
mnemonics, private keys, API tokens, logs, or database volumes. Preserve `.gitignore` and
`.dockerignore`. Bootstrap must keep asking before it replaces `.env`, because replacement
changes every identity.
