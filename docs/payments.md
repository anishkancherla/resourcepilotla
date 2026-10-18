# Payments and escrow

ResourcePilot uses the official Agent Payment Protocol, with the Orchestrator as `seller`:

```text
Orchestrator ─RequestPayment─▶ buyer
buyer ─CommitPayment─▶ Orchestrator   (or RejectPayment)
Orchestrator ─CompletePayment─▶ buyer (or CancelPayment with a reason)
```

## The business model

- Each seller keeps its own price. The buyer pays the sum of the sellers' quoted subtotals plus a
  visible booking fee: `PLATFORM_FEE_BPS` of the subtotal (default 3%) plus
  `PLATFORM_FEE_FIXED_CENTS` (default 30¢).
- The total goes to the Orchestrator's escrow, which is its own wallet, derived from
  `ORCHESTRATOR_SEED`.
- After every booking commits, the escrow ledger pays each seller exactly its subtotal and keeps the
  fee. If any booking fails, no seller is paid.
- Refundable deposits, such as a camera deposit, are shown on the cards and paid to the seller at
  pickup. They are not taken through escrow.

## Free study rooms are outside escrow

A Campus Rooms line is a $0 booking handoff. ResourcePilot re-checks that the room is open and
then gives the student the school's booking link; it never reserves or pays for the room.

- **Rooms only.** When every line in the checkout is a free handoff, there is no payment step.
  The commit round uses the reference `free:<workflow_id>`. The Orchestrator accepts it only
  when every hold is a $0 hold from a live Campus Rooms listing. No payment record, fee, or escrow
  entry is created, and demo sellers refuse a `free:` reference.
- **Mixed bundle.** The fee is computed on the paid lines only, and escrow covers only the paid
  lines. Payouts never include Campus Rooms. The usual rule still holds: no paid seller commits
  before the payment is verified and reserved.
- **Receipts.** Free lines appear under **Book it yourself**, with the school's link and a note
  that ResourcePilot did not reserve the room.

## The two rails

### Mock Stripe (`stripe_mock`)

This rail makes no Stripe API call, makes no chain transaction, and moves no money. It exists so the
whole checkout can be demonstrated without funds. `RequestPayment` metadata says
`stripe_mock=true` and `moves_funds=false`. A commit is accepted only if it meets all of these
conditions:

- The transaction ID starts with `STRIPE_MOCK:`.
- `stripe_mock_payment_intent` equals that transaction ID.
- `stripe_mock_buyer` is the requesting agent's address.
- `moves_funds` is `false`.
- The amount and recipient match.

Payouts and refunds on this rail are recorded as `simulated`, with IDs starting
`STRIPE_MOCK_PAYOUT:` or `STRIPE_MOCK_REFUND:`.

`make stripe-mock` runs [scripts/demo_payment_buyer.py](../scripts/demo_payment_buyer.py). It is a
buyer agent that submits the example demo-bundle form, clicks through the cards, and commits a
Mock Stripe payment.

### Test FET (`fet_direct`)

This rail uses faucet-issued `atestfet` on Fetch `stable-testnet`. The amount is the USD total ×
`FET_PER_USD`, a fixed demo rate and not a market price, capped at `FET_MAX_AMOUNT`. The buyer must
include `buyer_fet_wallet` in the commit metadata.

The Orchestrator queries the transaction with cosmpy. It accepts the commit only if the transaction
succeeded and contains a transfer from the buyer wallet to the escrow wallet of at least the
requested amount in `atestfet`. A transaction ID can pay only one reference.

Payouts and refunds are recorded as `scheduled` and nothing is sent, unless
`FET_PAYOUTS_ENABLED=true`. In that case, the Orchestrator sends stable-testnet transfers from its
escrow wallet. Fund that wallet from the faucet first to cover gas.

FET mainnet is permanently unsupported. Any other network or denom is refused when payment is
requested and again when it is verified.

## Verification before anything is booked

Every `CommitPayment` must match the stored payment record on all of these:

- reference
- requesting agent
- recipient
- currency
- payment method
- declared amount
- rail-specific evidence, as described above

Only then is the payment marked `verified`. It is moved to `reserved` exactly once, and only for the
workflow's active payment reference while that workflow is waiting for payment. Only a reserved
payment lets the Checkout Coordinator commit holds.

## Idempotency

- A duplicate `CommitPayment` returns `CompletePayment` and the same receipt. It never books
  again, and it never pays out again.
- Every escrow entry ID is `reference:kind:party`. Settlement can be retried safely, and an
  entry is executed only by whoever records it first.
- A retry after a failed booking re-reserves the same payment. The buyer is never asked to pay
  twice.
- Refunds move the payment to `refunded` once and record one refund entry.
