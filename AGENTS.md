# Instructions for AI coding assistants

Treat these as repository invariants. Before changing anything Fetch-facing, check the live
official pages in [docs/official-links.md](docs/official-links.md) for anything version-sensitive.

## Preserve the architecture

- Keep this repository standalone and based only on public Fetch.ai packages and documentation.
- Keep eight real platform uAgents: Orchestrator, Intake, Seller Discovery, Seller Verification,
  Logistics Analysis, Bundle Planner, Checkout Coordinator, and Memory and Audit. Each one needs
  a unique seed variable, a deterministic address, its own port and process, a profile README, a
  Chat Protocol manifest, a chat handler, a status card, and a meaningful protocol
  responsibility.
- Keep the demo sellers as separate uAgents with their own seeds, addresses, wallets, catalogs, and
  profile READMEs. Sellers must answer over `ResourceHold`; never read a seller's catalog from
  a platform agent.
- Never add padding roles, or simulate several roles inside one chatbot.
- The Orchestrator delegates with `ctx.send`. Keep workflow, request, requester, and session
  correlation, deadlines, duplicate protection, and the sender checks on every result and reply.
- Seller replies are authenticated by sender address, never by the `seller_id` they claim.
- Keep Postgres as the shared durable path. `MemoryStore` is for tests and development only.

## Preserve all-or-nothing checkout

- Never request payment before every seller in the bundle has confirmed a hold.
- Never commit a hold before the payment is verified and durably reserved.
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
