# Architecture

ResourcePilot is 13 independent uAgents that share one Postgres database. Eight platform agents own
the workflow. Five demo seller agents own their own inventory and answer only over messages.

## Protocols

| Protocol | Who speaks it | Messages |
| --- | --- | --- |
| Agent Chat Protocol | Every agent | `ChatMessage`, `ChatAcknowledgement` |
| `ResourcePilotTasks` 1.0.0 | Platform agents | `TaskRequest`, `TaskResult`, `AuditEvent` |
| `ResourceHold` 1.0.0 | Discovery and Checkout Coordinator (`buyer`), sellers (`seller`) | see below |
| Agent Payment Protocol | Orchestrator (`seller`), the buyer in ASI:One | `RequestPayment`, `CommitPayment`, `RejectPayment`, `CompletePayment`, `CancelPayment` |

`ResourceHold` is a typed `ProtocolSpecification` with two roles. Sellers implement `QuoteRequest`
to `Quote`, `HoldRequest` to `HoldConfirmed` or `HoldRejected`, `CommitHold` to
`BookingConfirmed` or `HoldRejected`, and `ReleaseHold`. The seller-role digest is what optional
Almanac discovery searches for.

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

## Recovery

| Failure | What happens |
| --- | --- |
| Seller does not quote | Discovery reports it after `QUOTE_WINDOW_SECONDS`; the carousel says which category is missing |
| Seller rejects, reprices, or ignores a hold | Every hold is released and nothing is charged. The Planner replans with `exclude_sellers`, and a new carousel shows the reroute. |
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

Payment states: `requested` → `verified` → `reserved` → `delivered`. A failed booking goes from
`reserved` to `released`, after which a retry returns it to `reserved` or a refund moves it to
`refunded`. A request the buyer abandons goes from `requested` to `cancelled`.
