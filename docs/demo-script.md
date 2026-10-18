# Demo script (about three minutes)

Before recording:

1. Run `make start` and connect all eight agents with Mailbox.
2. Open ASI:One and start a chat with the ResourcePilot Orchestrator.
3. Have a school email ready at a LibCal school for the room steps (for example Purdue, UCR, or
   UCLA). Know one school that gets the paste-link card (for example MIT), and have its library's
   room-booking link ready. Try both the day before: library sites change.
4. For the taken-room reroute, you need the slot to be taken between the carousel and the
   re-check. The honest way is to book that slot yourself on the school's booking page, in another
   tab with your own school sign-in, and cancel it after recording. If you cannot, use the test in
   step 3 instead.
5. For the optional emergency step, finish the setup in
   [agents/emergency/README.md](../agents/emergency/README.md) and set `EMERGENCY_DEMO_TO_NUMBER`
   to a teammate's phone. Place one test call the day before. Each call is billed to your
   ElevenLabs and phone-provider accounts. Skip the step if this is not set up.

The optional steps add about a minute.

## 1. The services menu (15 s)

Say "hi". The Orchestrator answers with the campus services menu: **Study rooms**, **Health &
wellness**, **Campus housing**, **Emergency (demo)**, and **My bookings**. The 988 crisis line is
at the bottom. Type a sentence instead, and it answers with the same menu: every request starts
from a button and a short form.

## 2. A real study room at any school (55 s)

Press **Open study rooms**. Enter your school email, a day, a time of day, a duration, and the
group size, then press **Find open rooms**.

While the progress card moves, say: "Intake keeps only the domain. The Campus Researcher matched
the school in IPEDS and found its LibCal site." For Purdue, that site lives at
`calendar.lib.purdue.edu`, not on `libcal.com`. "Campus Rooms, a separate agent, is reading which
rooms are open right now, and Verification checks every offer before the Planner ranks them."

The carousel shows real rooms, earliest first, each with a **Live · LibCal** badge. Press **View
room** on one, then **Re-check this room**. Campus Rooms reads that slot again, and the **Your
booking link** card arrives with the room, the time, and the school's own link. Say: "It's free,
and ResourcePilot never reserves the room. You book it with your own sign-in."

## 3. A room taken during the re-check (30 s)

Press **Find another room** and run the same search. Open a room, and before you re-check it, book
that slot yourself in the other tab. Back in ASI:One, press **Re-check this room**. The
Orchestrator says "Someone just booked that time; pick another room", the Planner runs again
without that room, and a new carousel shows the other open rooms. Say: "Only that room was
dropped, not the whole search." Cancel your booking in the other tab afterwards.

If you cannot take a slot live, show the same path in the test suite instead:

```bash
uv run pytest tests/test_network.py -k taken -v
```

## 4. No booking system found: the paste-link card (25 s)

Press **Find another room** and search with the MIT email. The Researcher finds no system it
recognizes, so it asks instead of guessing: a card with one field for the room-booking link.
Paste the library's
booking link and press **Check this link**. The Researcher checks the pasted link with the same
rules as one it found itself. Optional: paste a link on another domain first, and the same card
comes back with the reason.

Optional (30 s): press **Menu**, then **Open health & wellness** with a Purdue email. The card
starts with 988 and 911, then shows open wellness rooms from Purdue's LibCal and the week's health
events from `events.purdue.edu`, each with its RSVP link. Say: "It never asks about your health.
The crisis line is static, so it shows even when a school has nothing to find."

## 5. Campus housing (25 s)

Press **Menu**, then **Open campus housing**. Enter a Purdue, Georgia Tech, or UC Riverside email,
pick **Incoming first-year**, tick **Air conditioning**, and press **Find housing**. If payments are
set up, the hall-match offer appears first: press **Unlock my hall match** and pay in ASI:One's
payment panel with Stripe's test card `4242 4242 4242 4242` (any future date and CVC) or testnet
FET. Say: "The facts are free. The match is the one paid step, in test mode, and the Orchestrator
checks the payment with Stripe itself before the Planner starts." The carousel then
shows each hall the school lists, matches first, with photos where the school has them. Open one:
style, who it's for, rooms, features, price, and how it fits each criterion, with the apply link.
Say: "Every detail comes from the hall's own page, re-opened by Verification. If the page doesn't
say, it shows 'not listed'. You apply on the school's site." Press **Show all** to see the halls
your criteria ruled out, and **Housing facts** for IPEDS numbers and the portal.

## 6. Optional: Emergency (demo) (30 s)

Put your teammate's phone on speaker, near the microphone. Press **Menu**, then **Emergency
(demo)**. Pick a kind (for example fire or smoke), enter where on campus and how many people, and
submit the form. On the review card, point at "not 911" and the masked number, which shows only
the last four digits. Say: "It can only call our demo contact, and the voice opens by saying this
is a demo drill." Press **Call now**. The phone rings, the voice describes the emergency, and your
teammate answers as the dispatcher. After the call, press **Check the call** to show the summary
and transcript. Say: "One press places one call, and it is never retried. In a real emergency,
call 911."

## 7. My bookings (10 s)

Press **Menu**, then **My bookings**. The room from step 2 is there with its booking link.

## 8. An agent profile chat (15 s)

Open the Campus Rooms profile on Agentverse. Show its README and its Chat and Tasks manifests, then
send it a message. It answers with its status card, which names its role and has a **Use
ResourcePilot** button that points back to the Orchestrator. Say: "Eight separate agents, each
with its own address and job."

## 9. Close (5 s)

Say: "Free study rooms at any school, re-checked live, and housing facts with official links. The
only payment is the optional hall match, in test mode, and it never pays your school. No
reservations, and you always book with your own sign-in."
