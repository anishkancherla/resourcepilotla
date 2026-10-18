# Architecture

ResourcePilot is 15 independent uAgents that share one Postgres database. Nine platform agents own
the workflow. Six seller agents answer only over messages: five demo sellers own their own
inventory, and Campus Rooms reads real open study rooms from a school's public LibCal pages.

## Protocols

| Protocol | Who speaks it | Messages |
| --- | --- | --- |
| Agent Chat Protocol | Every agent | `ChatMessage`, `ChatAcknowledgement` |
| `ResourcePilotTasks` 1.0.0 | Platform agents | `TaskRequest`, `TaskResult`, `AuditEvent` |
| `ResourceHold` 1.1.0 | Discovery and Checkout Coordinator (`buyer`), sellers (`seller`) | see below |
| Agent Payment Protocol | Orchestrator (`seller`), the buyer in ASI:One | `RequestPayment`, `CommitPayment`, `RejectPayment`, `CompletePayment`, `CancelPayment` |

`ResourceHold` is a typed `ProtocolSpecification` with two roles. Sellers implement `QuoteRequest`
to `Quote`, `HoldRequest` to `HoldConfirmed` or `HoldRejected`, `CommitHold` to
`BookingConfirmed` or `HoldRejected`, and `ReleaseHold`. The seller-role digest is what optional
Almanac discovery searches for.

Version 1.1.0 adds an optional `campus` map of strings to `QuoteRequest` and `HoldRequest`. It
carries the school context the Campus Researcher found: domain, school name, timezone, booking
system, booking URL, LibCal host, group size, and room duration. Discovery sends it only to live
listings, so demo sellers never see it.

## Services and their chains

The Orchestrator answers every chat with a card. A greeting or any free text gets the services
menu; there is no sentence parser. Each menu button opens a form, and each form submission
starts a workflow for one service.

| Service | Specialist chain | Ends with |
| --- | --- | --- |
| `rooms` | Intake → Campus Researcher → Discovery → Verification → Analysis → Planner | A rooms carousel, then a free booking-link checkout |
| `wellness` | Intake → Campus Researcher → Discovery → Verification → Analysis → Planner | A wellness card: crisis line, open wellness rooms, the week's events (status `awaiting_selection`) |
| `wellness`, no room system | Intake → Campus Researcher → Verification | The same card without rooms (status `answered`) |
| `housing` | Intake → Campus Researcher → Verification | A housing card (status `answered`) |
| `bundle` | Intake → [Campus Researcher] → Discovery → Verification → Analysis → Planner | A bundle carousel, then escrow checkout |

A bundle asks the Researcher only when the form adds a free study room and a school email. A chain
that ends at Verification is an answer (`answered`); one that ends at the Planner has options to
pick from (`awaiting_selection`).

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
the same rules. A link that fails the checks returns the same card with the reason. In a bundle,
the student can instead skip the room.

For housing it reads IPEDS institutional characteristics and finds the housing office page. The
portal must be a link on the office's own pages, or on its housing vendor (StarRez), that reads
like an application or portal. Seller Verification then re-opens each official link itself and
drops any that do not open.

For wellness it finds the room system the same way but never pauses for a link: rooms are
optional on the wellness card. It also looks for the school's public Localist calendar at
`events.<domain>/api/2/events`, then `calendar.<domain>`, then `<school>.localist.com` (accepted
only when its home page links back to the school). It keeps up to eight events in the next seven
days whose title, keywords, tags, or filter names read as health or wellness, with only a capped
title, start time, location, and link. A missing or failing feed is shown on the card, never a
failed request. Campus Rooms then quotes `wellness_room` spaces from LibCal. If there are none,
Verification, Analysis, and the Planner pass the reason through instead of failing. Verification
also re-fetches the feed itself and drops events it no longer lists. Picking a room uses the same
free re-check and booking-link checkout as study rooms.

Results are cached per domain in `api_cache`: a week when found, a day otherwise. A week's event
list is cached for 30 minutes. The fetch rules
are in [trust-model.md](trust-model.md).

## The planning pipeline

```text
Orchestrator ─TaskRequest─▶ Intake ─TaskResult─▶ Orchestrator
             ─TaskRequest─▶ Seller Discovery ═QuoteRequest═▶ sellers ═Quote═▶ Discovery
             ◀─TaskResult── Discovery (after every seller answers or QUOTE_WINDOW_SECONDS)
             ─TaskRequest─▶ Seller Verification ─▶ Logistics Analysis ─▶ Bundle Planner
             ◀─TaskResult── Bundle Planner: up to three bundles, then a carousel card
```

Each `TaskRequest` carries the workflow ID, a fresh request ID, the requester, the session, the
target role, the payload, and a deadline. The Orchestrator accepts a `TaskResult` only when all of
these hold:

- The sender is the registered address of the claimed source role.
- The result is the first one seen for that request ID.
- The request ID is the one the workflow currently expects.
- The workflow is at that role's stage.

Late, duplicate, and forged results are ignored.

## Fan-out rounds

Discovery and the Checkout Coordinator use durable fan-out rounds stored in `fanout_rounds`. A
round has an owner role, a deadline, the expected participants, and their replies. A reply is
recorded only if three things hold:

- The round is still open.
- The sender is the address the round was sent to.
- No reply from that sender has been recorded yet.

A round closes exactly once, either when it completes or when the owner's two-second sweep finds it
past its deadline. Because rounds live in Postgres, a restarted agent finishes its open rounds.

## Checkout

1. **Hold round.** The Checkout Coordinator sends one `HoldRequest` per bundle line, addressed to
   the seller's registry address. Each seller places a time-limited hold under a Postgres advisory
   lock, so capacity can never be oversold.
   - The round succeeds only if every seller confirms, at the price it quoted.
   - One rejection ends the round at once, and silence ends it at `HOLD_WINDOW_SECONDS`.
   - On failure, every hold in the round is released, and a confirmation that arrives late is
     released as soon as it lands.
2. **Review and payment.** The Orchestrator shows a review card with the hold expiry. It then sends
   `RequestPayment` for the bundle total, to escrow.
3. **Verify and reserve.** The Orchestrator verifies `CommitPayment` and moves the payment from
   `verified` to `reserved` exactly once. Only the first verified commit for the active reference
   proceeds.
4. **Commit round.** The Checkout Coordinator sends `CommitHold` to every seller, and the round
   succeeds only if every seller confirms. On failure, every hold is released, including committed
   ones, so nothing is half-booked.
5. **Settle.** The Orchestrator writes seller payouts and the fee to the escrow ledger, marks the
   payment delivered, and sends a terminal receipt card.

### Free study-room lines

A Campus Rooms line costs $0 and is a booking handoff, not a reservation.

- **Hold.** The hold is a live re-check: Campus Rooms reads LibCal's availability for that exact
  slot and confirms only if it is still open. It records a marker hold so two ResourcePilot
  students are not handed the same slot at once.
- **Rooms only.** If every line is a free handoff, there is no payment step. The review card offers
  **Get booking link**. The commit round then uses the reference `free:<workflow_id>`. The
  Orchestrator accepts that reference only when every hold is a $0 Campus Rooms hold, and demo
  sellers refuse it. No payment record or escrow entry is created.
- **Mixed bundle.** The free line joins the hold round with the paid lines. Escrow, the fee, and
  every payout cover only the paid lines, and no paid seller commits before the payment is
  verified and reserved. Campus Rooms answers the commit with the booking link and never fails a
  paid bundle.

The receipt lists each free line under **Book it yourself**, with the school's link.

## Recovery

| Failure | What happens |
| --- | --- |
| Seller does not quote | Discovery reports it after `QUOTE_WINDOW_SECONDS`; the carousel says which category is missing |
| Seller rejects, reprices, or ignores a hold | Every hold is released and nothing is charged. The Planner replans with `exclude_sellers`, and a new carousel shows the reroute. |
| A study room is taken during the re-check | Only that room is dropped (`exclude_items`), not Campus Rooms, and the remaining open rooms are shown |
| The Researcher finds no booking system | The workflow waits at `awaiting_link` for a pasted link, or a bundle continues without the room |
| LibCal's availability endpoint fails | Campus Rooms offers the school's booking link only, never invented rooms |
| Seller fails to commit after payment | Every booking is cancelled and the payment is released back to `released` in escrow. A recovery card offers **Retry booking** (re-hold, then commit with the same payment) or **Refund me**. |
| Buyer waits after a failed booking | The Orchestrator refunds automatically after `PAYMENT_DEADLINE_SECONDS` |
| A specialist task is lost | The Orchestrator retries it once with a fresh request ID, then fails with a clear card |
| Checkout Coordinator is down during booking | The payment is kept in escrow and the recovery card is shown |
| A success arrives after the workflow moved on | Its holds or bookings are released |

Every failed hold or commit, and every silence, updates the seller's track record. Verification
reads that record on the next request.

## State

| Table | Holds |
| --- | --- |
| `workflows` | One JSON state per workflow: stage, status, correlation, bundle, holds, receipt |
| `idempotency_keys` | One-time claims for chat messages, results, hold attempts, and timeouts |
| `workflow_events` | The append-only audit timeline written by Memory and Audit |
| `user_preferences` | Saved priority, anchor, and training status per requester |
| `fanout_rounds` | Quote, hold, and commit rounds with replies |
| `seller_holds` | Each seller's holds and bookings, with capacity enforced under an advisory lock |
| `payments` | Payment references and their state machine; transaction IDs are unique |
| `escrow_entries` | Payouts, the fee, and refunds, each with a deterministic ID |
| `seller_stats`, `seller_settings` | Track-record counters and demo fault modes |
| `api_cache` | Cached public lookups (IPEDS, Researcher results, LibCal pages) with an expiry |

Payment states: `requested` → `verified` → `reserved` → `delivered`. A failed booking goes from
`reserved` to `released`, after which a retry returns it to `reserved` or a refund moves it to
`refunded`. A request the buyer abandons goes from `requested` to `cancelled`.
