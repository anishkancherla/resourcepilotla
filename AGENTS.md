# Instructions for AI coding assistants

Treat these as repository invariants. Before changing anything Fetch-facing, check the live
official pages in [docs/official-links.md](docs/official-links.md) for anything version-sensitive.

## Preserve the architecture

- Keep this repository standalone and based only on public Fetch.ai packages and documentation.
- Keep eight real uAgents, all run by `agents/run.py` with `AGENT_ROLE=<key>`:

  | Key | Agent | Port | Responsibility |
  | --- | --- | --- | --- |
  | `orchestrator` | ResourcePilot Orchestrator | 8000 | Runs the ASI:One chat, the services menu, cards, and the workflow |
  | `intake` | ResourcePilot Intake | 8001 | Turns a submitted form into a validated brief, keeping only the school domain |
  | `researcher` | ResourcePilot Campus Researcher | 8002 | Finds a school's room system, housing pages, and events feed from its email domain |
  | `campus_rooms` | ResourcePilot Campus Rooms | 8003 | Reads open study and wellness rooms live and re-checks one before handing over its link |
  | `verification` | ResourcePilot Verification | 8004 | Checks every room offer and re-opens housing links and event feeds before you see them |
  | `planner` | ResourcePilot Planner | 8005 | Ranks open rooms and matches campus housing to your criteria |
  | `memory_audit` | ResourcePilot Memory and Audit | 8006 | Keeps the append-only audit timeline and your recent bookings |
  | `emergency` | ResourcePilot Emergency Dispatcher | 8007 | Places one demo-drill call to your team's demo contact, never 911, and reports the transcript |

  Each one needs a unique seed variable keyed by its key (`<KEY>_SEED`), a deterministic address,
  its own port and process, a profile README at `agents/<key>/README.md`, a Chat Protocol
  manifest, a chat handler, a status card, and a meaningful protocol responsibility. Never change
  a key: seeds and addresses in users' `.env` files are keyed by it.
- Each service has its own specialist chain, and the Orchestrator runs it in order:
  - `rooms`: Intake → Campus Researcher → Campus Rooms → Verification → Planner, then a rooms
    carousel. "Re-check this room" sends Campus Rooms a `phase="recheck"` task. Still open: the
    booking-link card. Taken: the Planner again with that room in `exclude_items`.
  - `wellness`: the `rooms` chain for wellness rooms, then the wellness card (988 first, open
    wellness rooms, the week's events). The Researcher never pauses for a link here; a school
    with no room system skips Campus Rooms and the Planner (Intake → Campus Researcher →
    Verification). Verification re-fetches the events feed itself. Picking a room uses the same
    re-check and booking link as study rooms.
  - `housing`: Intake → Campus Researcher → Verification → Planner, then the halls carousel (or
    the housing facts card when no hall could be read). The Researcher reads each hall from the
    school's own pages; Verification re-opens every hall page; the Planner matches halls to the
    student's criteria. Halls a page rules out are hidden; unknowns are shown after matches.
  - `emergency`: Emergency Dispatcher (`phase="preview"`) → review card (`awaiting_call`).
    **Call now** sends the Dispatcher a `phase="call"` task, then the call card (`called`).
    **Check the call** sends a `phase="status"` task and shows the transcript.
- The Orchestrator offers services from a menu: Study rooms, Health & wellness, Campus housing,
  Emergency (demo), and My bookings.
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
  From a school's public events feed, keep only a capped title, start time, location, and event
  link, shown as data and never acted on. From a school's own hall pages, keep only a capped name,
  an `og:image` photo on the school's domain, style, who it is for, room types, listed features,
  the listed price, and a short "about" line that holds no address, link, or instruction-like
  text; all of it is shown as data and never acted on.
- If a booking system is not found, ask the student for the link and verify it the same way.
  Never guess a booking system, invent rooms, or claim a room was reserved.
- Re-check a room live before handing over its booking link. ResourcePilot never reserves a room;
  the student books with their own sign-in.
- Housing is facts, official links, and the halls the school lists on its own site, matched to
  the student's criteria. Never infer what a page does not say: a hall is ruled out only by its
  own page. Never apply for, reserve, or pay for housing.
- Wellness is free room handoffs, public events, and links. Never ask about a student's health,
  store health details, book health or counseling appointments, or give health advice. Every
  wellness card and the menu carry the static 988 and 911 crisis line, which never depends on a
  crawl or a feed.

## Preserve emergency-call safety

- The emergency service is a demo drill, never a real emergency call. Never dial 911, campus
  police, or any emergency service.
- Take the destination only from `EMERGENCY_DEMO_TO_NUMBER`. Nothing in a chat message, form, or
  task payload may choose or change the number.
- Refuse 911, 112, 999, 988, N11 codes, the listed crisis hotlines, short numbers, and anything that
  is not a full phone number with a country code.
- The voice's opening line always starts with "this is a ResourcePilot demo drill, not a real
  emergency".
- Place at most one call per workflow. The Orchestrator claims **Call now** once, and the
  Dispatcher claims the call once. Never retry a call, including after a timeout: the workflow
  fails and the student starts a new demo.
- Start a call only from the **Call now** selection with its stable workflow ID, never from prose.
- Cap free text and remove email addresses before anything is sent to ElevenLabs.
- Cards show only the last four digits of the number. Every emergency card says it is a demo drill
  that does not contact 911 or campus police, and "In a real emergency, call 911."
- Never log, show, or put in an error the ElevenLabs API key, the full number, or a raw response
  body.
- Only the Emergency Dispatcher talks to ElevenLabs, over https to `api.elevenlabs.io` or
  `api.<region>.elevenlabs.io`.
- Never claim a live call was tested or demonstrated without real evidence. The tests mock
  ElevenLabs.

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
