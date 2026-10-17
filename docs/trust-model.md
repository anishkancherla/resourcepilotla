# Trust model

Why can an Orchestrator send your money toward a seller it did not build? Because identity, conduct,
and money are each checked in a different place.

## 1. Where sellers come from

**Curated registry.** [resourcepilot/registry.py](../resourcepilot/registry.py) lists each seller's
ID, display name, categories, location, opening hours, and identity status. The registry stores a
seller's agent address and payout wallet as environment variables derived from the seller's seed.
The registry is the only place a seller's identity is asserted.

**Optional open discovery.** With `ALMANAC_DISCOVERY=true`, Seller Discovery also calls
`ctx.get_agents_by_protocol` with the `ResourceHold` seller digest. It then asks up to five
unlisted agents for quotes. Their quotes reach Verification so the rejection is visible, but an
unlisted seller can never pass the identity check. Discovering a seller is not the same as trusting
it.

## 2. Four vetting layers

Seller Verification runs these checks on every quote, in order. A rejection names the layer and the
reason, and the **Why not others?** card shows them.

1. **Identity.** The quote must arrive from the exact registry address of a listing marked
   identity-verified. Quotes are keyed by the authenticated sender address, so a seller cannot
   claim another seller's ID.
2. **Protocol conformance.** These must all hold:
   - Protocol version 1.0.0 and currency USD.
   - A hold time of at least 120 seconds.
   - Offers only in the seller's listed categories.
   - `subtotal = unit price × quantity × hours`, within one cent.
3. **Track record.** Memory and Audit counts each seller's quotes, silences, held and failed holds,
   and committed and failed bookings. A seller with at least three attempts and a failure rate above
   50% is dropped. A seller with fewer than three committed bookings is "new", and any single line
   it offers above `NEW_SELLER_CAP_CENTS` is rejected.
4. **Policy.** The item must be available for the requested window and quantity. Items that require
   training are dropped unless you said you have it.

After vetting, Logistics Analysis drops offers that cannot be ready in time. The Bundle Planner
never builds a bundle over your budget.

## 3. Conduct at checkout

Vetting judges what a seller promised. Checkout judges what the seller does:

- A hold confirmed at a different price than quoted is treated as a rejection.
- Holds and commits are sent only to registry addresses. Replies from any other sender are ignored.
- One failing seller releases everyone's holds, and the Planner reroutes around that seller.

## 4. Money stays with the platform until the end

Buyers pay the Orchestrator's escrow, never a seller. Sellers are paid their quoted subtotal only
after every booking in the bundle commits. A seller that misbehaves at the last step gets nothing,
and the buyer can retry or take a refund.

## Limits of this demo

- The five sellers are demo businesses. Their "verified" flags are demo onboarding markers, not the
  result of a real business-identity check.
- A production registry would add domain or business verification, signed listings, dispute
  handling, and payout holds for new sellers.
- Almanac discovery is off by default and has been tested only against a simulated Almanac.
