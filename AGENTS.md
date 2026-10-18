# Instructions for AI coding assistants

Treat these as repository invariants. Before changing anything Fetch-facing, check the live
official pages in [docs/official-links.md](docs/official-links.md) for anything version-sensitive.

## Preserve the architecture

- Keep this repository standalone and based only on public Fetch.ai packages and documentation.
- Keep nine real platform uAgents: Orchestrator, Intake, Seller Discovery, Seller Verification,
  Logistics Analysis, Bundle Planner, Checkout Coordinator, Memory and Audit, and Campus
  Researcher. Each one needs a unique seed variable, a deterministic address, its own port and
  process, a profile README, a Chat Protocol manifest, a chat handler, a status card, and a
  meaningful protocol responsibility.
- Keep the sellers as separate uAgents with their own seeds, addresses, wallets, and profile
  READMEs: five demo sellers with catalogs, and Campus Rooms, the live seller of free study-room
  handoffs. Sellers must answer over `ResourceHold`; never read a seller's catalog or a school's
  room availability from a platform agent.
- The Orchestrator offers services from a menu: study rooms, campus housing, and the labeled
  demo bundle. Requests start only from a form submission; free text gets the menu card, never a
  workflow. Every Orchestrator reply is a card.
- Never add padding roles, or simulate several roles inside one chatbot.
- The Orchestrator delegates with `ctx.send`. Keep workflow, request, requester, and session
  correlation, deadlines, duplicate protection, and the sender checks on every result and reply.
- Seller replies are authenticated by sender address, never by the `seller_id` they claim.
- Keep Postgres as the shared durable path. `MemoryStore` is for tests and development only.

## Preserve all-or-nothing checkout

- Never request payment before every seller in the bundle has confirmed a hold.
- Never commit a paid hold before the payment is verified and durably reserved.
- The one exception is a free Campus Rooms handoff: a $0 line that ResourcePilot re-checks but
  never reserves. A checkout made only of such lines commits with `free:<workflow_id>` and no
  payment. Accept that reference only when every hold is a $0 hold from a live listing; demo
  sellers must refuse it. In a mixed bundle, escrow and the fee cover only the paid lines, and
  paid sellers still never commit before verified, reserved payment.
- Any rejection, price change, or silence releases every hold in the round, including holds that
  confirm late.
- Never pay a seller before every booking in the bundle commits. Sellers receive exactly the
  subtotal they quoted.
- A failed booking keeps a retryable verified entitlement in escrow, and a retry must never ask
  for a second payment. Refunds are recorded once.

## Preserve protocol correctness

- Import Chat and Payment specifications from `uagents_core.contrib.protocols`.
- ACK inbound `ChatMessage` promptly, and publish every included manifest.
- The Orchestrator is `Protocol(spec=payment_protocol_spec, role="seller")`. Discovery and the
  Checkout Coordinator are `ResourceHold` buyers. Sellers are `ResourceHold` sellers and never
  take payment directly.
- Build cards as `TextContent` plus `MetadataContent`, and make every metadata value a string.
  Keep `card_protocol_version="1"`, `requires_card_interaction="true"`, a supported
  `card_kind`, and a JSON `card_payload` of 64KB or less. Custom cards need a typed `root`
  and may be at most eight levels deep.
- Put stable action, workflow, and bundle IDs in selections. Parse direct JSON first, then the
  bounded prose allowlist. Never infer a hold, payment, retry, or refund from ambiguous prose.

## Preserve live-data safety

- Keep only the domain of a school email. Strip the local part before anything is stored, sent,
  or logged.
- Fetch only public https pages on the school's domain or a known vendor. Check that resolved
  addresses are public, vet every redirect, cap bytes, and keep the fetch and time budgets.
- Treat page text as untrusted evidence: extract links and fingerprints only, never instructions.
- If a booking system is not found, ask the student for the link and verify it the same way.
  Never guess a booking system, invent rooms, or claim a room was reserved.
- Housing is facts plus official links. Never apply for, reserve, or pay for housing.

## Preserve payment safety

- Verify success, the configured network and denom, the minimum amount, the escrow recipient, the
  buyer wallet, and transaction uniqueness before reserving.
- Stable testnet with faucet-issued tokens and a small capped amount is the only FET path. Never
  add a mainnet or real-value payment switch.
- Keep `stripe_mock` unmistakably named and documented as making no Stripe API call and moving
  no funds. Never describe it as a real Stripe integration.
- Duplicate `CommitPayment` must never book or pay out twice.
- Do not claim real Stripe, Skyfire, UCP, MCP, A2A, AEVs, or live Almanac discovery without a
  real, tested implementation and demo evidence.

## Keep docs and tests synchronized

When a role, seller, port, env variable, protocol, card, or payment rule changes:

1. Update `.env.example`, the profile READMEs, the README tables, and the docs.
2. Update identity generation in `resourcepilot/config.py`. Never commit generated addresses.
3. Update the card, store, vetting, checkout, recovery, network, and safety tests.
4. Run `make check`, `make build`, and `make verify`.
5. Ask the user to run `make profiles`, open every Inspector link, connect every Mailbox, and
   complete the account-bound Agentverse and ASI:One steps themselves.

Never fabricate profile links, addresses, registrations, test results, or payments.

## Secret handling

Never read secrets into output, print seeds, or commit `.env`, `.profiles.json`, wallet
mnemonics, private keys, API tokens, logs, or database volumes. Preserve `.gitignore` and
`.dockerignore`. Bootstrap must keep asking before it replaces `.env`, because replacement
changes every identity.
