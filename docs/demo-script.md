# Demo script (about three minutes)

Before recording:

1. Run `make start` and connect all 13 agents with Mailbox.
2. Set `SELLER_DEMO_CONTROLS=true` in `.env`, then restart with `make stop` and
   `make start`. The flag adds Simulate outage and Restore service buttons to each seller's own
   profile chat.
3. Open ASI:One and start a chat with the ResourcePilot Orchestrator.

## 1. One sentence, many sellers (45 s)

Send:

> I need a DSLR, a podcast room, and 20 color posters Friday from 3-6 PM, under $35

While the progress card shows the stages, say: "Seven specialist agents are working, and Discovery
is getting live quotes from five independent seller agents."

The carousel arrives. Point out the narration: **Excluded for identity: Flash Prints 4U.** It was the
cheapest print shop, but it has no verified identity.

## 2. The trust gate (30 s)

Open a bundle, then press **Why not others?** The card shows each rejection with its layer:

- **Identity:** Flash Prints 4U is unverified.
- **Policy:** the DSLR requires gear-share training, so the bundle substitutes a mirrorless kit.

Say: "Nothing reaches you that failed identity, protocol, track record, or policy."

## 3. All-or-nothing, with a live failure (60 s)

In another tab, open Westwood Print Lab's profile chat and press **Simulate outage**. Back in the
Orchestrator chat, press **Hold everything** on the Lowest price bundle.

After about 15 seconds:

- "Westwood Print Lab could not hold your items. Nothing was charged, and every other hold was
  released."
- A new carousel arrives: **Rerouted around: Westwood Print Lab.** Gayley Copy Center now
  supplies the posters.

## 4. Pay once, into escrow (45 s)

Open the rerouted Lowest price bundle and press **Hold everything**. The review card shows that
every seller is holding, and when the holds expire. Press **Choose payment**: the payment card
offers **Mock Stripe**, which moves no money, and **Pay test FET**. The Orchestrator then sends an
official `RequestPayment`.

ResourcePilot does not control whether ASI:One itself can answer a `stripe_mock` request. To show
a complete no-money booking, run `make stripe-mock` in a terminal. That starts a separate buyer
agent that books its own bundle through the same cards and commits a Mock Stripe payment. For the
Fetch-native rail, pay the requested amount from a faucet-funded stable-testnet wallet.

The receipt card shows:

- one confirmation code per seller
- the payment rail
- each seller's payout from escrow
- the booking fee ResourcePilot kept

## 5. Close (10 s)

Say: "Thirteen agents, one payment, and nobody paid until everybody delivered." Then press **My
bookings**.

Afterwards, restore Westwood Print Lab from its profile chat.
