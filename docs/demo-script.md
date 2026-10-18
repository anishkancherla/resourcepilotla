# Demo script (about three and a half minutes)

Before recording:

1. Run `make start` and connect all 15 agents with Mailbox.
2. Set `SELLER_DEMO_CONTROLS=true` in `.env`, then restart with `make stop` and
   `make start`. The flag adds Simulate outage and Restore service buttons to each demo seller's
   own profile chat.
3. Open ASI:One and start a chat with the ResourcePilot Orchestrator.
4. Have a school email ready. Use one at a LibCal school for the room step (for example
   Purdue, UCR, or UCLA), and know one school that needs the paste-link card (for example MIT).

## 1. The services menu (15 s)

Say "hi". The Orchestrator answers with the campus services menu: **Study rooms**, **Health &
wellness**, **Campus housing**, **Project bundle**, and **My bookings**. The 988 crisis line is at
the bottom. Type a sentence instead, and it answers with
the same menu: every request starts from a button and a short form.

## 2. A real study room at any school (60 s)

Press **Open study rooms**. Enter your school email, a day, a time of day, a duration, and the
group size, then press **Find open rooms**.

While the progress card moves, say: "Intake keeps only the domain. The Campus Researcher matched
the school in IPEDS and found its LibCal site." For Purdue, that site lives at
`calendar.lib.purdue.edu`, not on `libcal.com`. "Campus Rooms, a separate seller agent, is
reading which rooms are open right now."

The carousel shows real rooms, earliest first, each with a **Live · LibCal** badge. Open one and
press **Re-check this room**. The review card says **Still open**; press **Get booking link**.
The receipt is the school's own booking link. Say: "It's free, and ResourcePilot never reserves
the room. You book it with your own sign-in."

Optional: repeat with an MIT email. The Researcher finds no system it recognizes, so it asks for
the link instead of guessing.

Optional (30 s): press **Open health & wellness** with a Purdue email. The card starts with 988 and
911, then shows open wellness rooms from Purdue's LibCal (for example a sensory room) and the
week's health events from `events.purdue.edu`, each with its RSVP link. Say: "It never asks about
your health. The crisis line is static, so it shows even when a school has nothing to find."

## 3. Campus housing (30 s)

From the menu, press **Open campus housing** and enter the same email. The card shows IPEDS
facts (dorm beds, typical room and board, meal plans), the housing office, and the application
portal. Point at **Sources**: "Seller Verification re-opened each official link itself."

## 4. The project bundle: many sellers, one payment (60 s)

Press **Open project bundle**. The form says plainly that these are demo shops near UCLA. Pick a
DSLR, the podcast studio, and 20 posters on Friday from 3 PM for 3 hours, with a $35 budget.

The carousel narration says **Excluded for identity: Flash Prints 4U.** It was the cheapest print
shop, but it has no verified identity. Open a bundle and press **Why not others?**:

- **Identity:** Flash Prints 4U is unverified.
- **Policy:** the DSLR requires gear-share training, so the bundle substitutes a mirrorless kit.

In another tab, open Westwood Print Lab's profile chat and press **Simulate outage**. Back in the
Orchestrator chat, press **Hold everything**. After about 15 seconds a new carousel arrives:
**Rerouted around: Westwood Print Lab.** Gayley Copy Center now supplies the posters, and nothing
was charged.

## 5. Pay once, into escrow (35 s)

Open the rerouted bundle and press **Hold everything**. The review card shows that every seller
is holding, and when the holds expire. Press **Choose payment**. The card offers **Mock Stripe**,
which moves no money, and **Pay test FET**. The Orchestrator then sends an official
`RequestPayment`.

ResourcePilot does not control whether ASI:One itself can answer a `stripe_mock` request. To show
a complete no-money booking, run `make stripe-mock` in a terminal. That starts a separate buyer
agent that books its own bundle through the same cards and commits a Mock Stripe payment. For the
Fetch-native rail, pay the requested amount from a faucet-funded stable-testnet wallet.

The receipt shows one confirmation code per seller, the payment rail, each seller's payout from
escrow, and the booking fee ResourcePilot kept.

## 6. Close (10 s)

Say: "Fifteen agents. Free rooms at any school, housing facts with official links, and one payment
for many sellers, with nobody paid until everybody delivered." Then press **My bookings**.

Afterwards, restore Westwood Print Lab from its profile chat.
