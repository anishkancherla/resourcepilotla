# ResourcePilot: the campus team that finds you a room, a hall, and help in your first weeks

This isn't a chatbot. It's a team. Type your school email into one ASI:One conversation, and eight
Fetch.ai agents find your school's room system, read which study rooms are open right now, check
every offer, re-open every housing page, and hand you the school's own link to book it yourself.
It works at any US school, not just one campus.

## 🔗 Try it

- **ASI:One:** chat with the ResourcePilot Orchestrator: [ASI:One chat link]
- **Demo video:** [YouTube link]
- **GitHub:** https://github.com/kevinjylin/resourcepilotla

Say "hi" and you get the services menu:

> 📚 **Study rooms** · 🧘 **Health & wellness** · 🏠 **Campus housing** · 📞 **Emergency (demo)** · 🗂 **My bookings**

## 🤖 8 agents on Agentverse

| Agent | Job | Address |
| --- | --- | --- |
| **Orchestrator** | Runs the ASI:One chat, the services menu, every card, and the workflow | [agent1q…] |
| **Intake** | Turns a submitted form into a validated brief and keeps only your school's domain | [agent1q…] |
| **Campus Researcher** | Finds your school's room-booking system, housing pages, and events feed from its email domain | [agent1q…] |
| **Campus Rooms** | Reads open study and wellness rooms live, and re-checks one before you get its link | [agent1q…] |
| **Verification** | Checks every room offer, and re-opens every housing page and events feed before you see them | [agent1q…] |
| **Planner** | Ranks open rooms and matches campus halls to what you need | [agent1q…] |
| **Memory & Audit** | Keeps the append-only audit timeline and your recent bookings | [agent1q…] |
| **Emergency Dispatch** | Places one demo-drill phone call to our team's demo contact, never 911, and reports the transcript | [agent1q…] |

Each one is a real uAgent with its own seed, address, port, process, profile README, Chat Protocol
manifest, and status card. Message any specialist on Agentverse and it answers with a card that
names its job and points you back to the Orchestrator.

## 💡 Inspiration

The first weeks at a new school are a pile of small questions nobody answers in one place.

Where can I study with my group tonight? Every library has a room-booking system, but every school
hides it somewhere different. UCLA's is on LibCal. Purdue's lives at `calendar.lib.purdue.edu`.
Some schools use EMS or 25Live, and some don't link it from the library home page at all.

Which hall should I live in? Housing sites list a dozen buildings across a dozen pages, with
prices tucked into rates tables and PDFs, and "does it have A/C?" takes twenty minutes of
clicking.

Where do I go when I'm overwhelmed? Wellness rooms and events exist, but they're buried in
calendars most students never open.

[Add your own story here: the moment one of you hit this as a new student.]

Campus "welcome week" agents usually solve this for one school, with a hand-built list. We wanted
one that works for **any** US school, from nothing but an email address, and that never makes
anything up.

## ⚡ What it does

Everything starts from a button and a short form. Free text gets the menu back, so the agents
never guess what you meant.

### 📚 Study rooms

Press **Open study rooms**, enter your school email, a day, a time, how long, and your group size.

1. **Intake** strips your email down to the domain (`purdue.edu`). Your name never leaves the form.
2. **Campus Researcher** matches the domain to your school in IPEDS, then finds the library's
   booking system: a hand-checked list, then a `<school>.libcal.com` probe, then a short crawl of
   the library site that catches custom domains like Purdue's.
3. **Campus Rooms** reads which rooms are open right now, live from LibCal.
4. **Verification** drops any offer that isn't available, isn't on your school's domain, falls
   outside your time window, or doesn't fit your group.
5. **Planner** ranks what's left, earliest first.

You get a carousel of real rooms, each with a **Live · LibCal** badge. Pick one and press
**Re-check this room**: Campus Rooms reads that slot again, uncached. Still open? You get your
school's booking link. Taken in the meantime? The Planner runs again without that room, and you
get the rest.

Rooms are always free, and **ResourcePilot never reserves anything**. You book with your own
sign-in.

If the Researcher can't find a booking system, it doesn't guess. It asks you for the link and
checks it with the same rules as one it found itself.

### 🧘 Health & wellness

The same chain finds open meditation, reflection, and sensory rooms at your school, plus the
week's health and wellness events from the school's public calendar, each with its photo and RSVP
link. The 988 and 911 crisis lines sit at the top of every wellness card. They're static text, so
they show up even when a school has nothing to find. ResourcePilot never asks about your health.

### 🏠 Campus housing

Tell it who you are (first-year, transfer, continuing, graduate, family), the style you want,
your must-haves (A/C, kitchen, private bath, gender-inclusive, accessible), and an optional
budget.

- The **Researcher** reads every hall your school lists on its own housing site: its photo, style,
  who it's for, room types, features, and price.
- Prices come from the hall's own page, the school's rates table, or the hall's rate PDF. When a
  school publishes none, the card shows the school's IPEDS typical cost, clearly labeled as a
  school-wide figure.
- **Verification** re-opens every hall page and official link itself.
- The **Planner** marks each criterion as a match, not listed, or ruled out, and only a hall's own
  page can rule it out.

You get a carousel of halls with matches first, and the apply link to your school's portal.

### 📞 Emergency (demo)

A demo drill of what an agent-placed emergency call could look like. Pick what's happening, where
on campus, and how many people. The review card shows the call before it happens. Press **Call
now**, and the Emergency Dispatcher places one ElevenLabs voice call to our team's demo contact.
The voice opens with "this is a ResourcePilot demo drill, not a real emergency". Afterwards,
**Check the call** shows the summary and transcript.

It **never** calls 911, campus police, or any emergency service. The number comes only from our
server config, nothing in chat can change it, and 911, 988, N11 codes, and crisis hotlines are
refused outright.

### 🗂 My bookings

Your recent re-checked rooms and their booking links, read from the shared workflow store.

## 🏗 Architecture

```text
  YOU                 ORCHESTRATOR                  SPECIALISTS (ctx.send, in order)       LIVE SOURCES
  ───                 ────────────                  ────────────────────────────────       ────────────
                                              ┌──▶ ① Intake ─────────────────────▶ (domain only)
  ASI:One ──────▶  Orchestrator (uAgent)      ├──▶ ② Campus Researcher ──────────▶ IPEDS · library sites
  (Chat Protocol)  · services menu            │                                     housing pages · Localist
                   · picture cards            ├──▶ ③ Campus Rooms ───────────────▶ LibCal (live)
                   · workflow state           ├──▶ ④ Verification ───────────────▶ re-opens links + feeds
                   · re-check + reroute       ├──▶ ⑤ Planner
                          │                   └──▶ Emergency Dispatch ──────────▶ ElevenLabs ⇄ Twilio / SIP
                          ▼
                   Memory & Audit ──▶ Postgres (workflows, audit timeline, one-time claims, cache)
```

How a request moves:

1. Your form arrives at the Orchestrator over the Agent Chat Protocol.
2. The Orchestrator hands each step to the next specialist with `ctx.send` and a `TaskRequest` on
   our own `ResourcePilotTasks` protocol. Every task carries workflow, request, requester, and
   session IDs, a deadline, and duplicate protection.
3. The Orchestrator accepts only the matching `TaskResult` from the agent it asked.
4. A specialist that goes silent is retried once with a fresh request ID, then the workflow fails
   with a clear card.
5. Every step lands on one append-only audit timeline, so a single workflow ID traces Intake, the
   Researcher, Campus Rooms, Verification, the Planner, the re-check, and the booking link.

## 🏁 How ResourcePilot meets the Fetch.ai judging criteria

| Criterion | What ResourcePilot does |
| --- | --- |
| **Multi-agent collaboration** | 8 uAgents, each its own process and address, each owning one step. Only Campus Rooms reads room availability; only Emergency Dispatch talks to ElevenLabs. Not one chatbot wearing eight hats. |
| **ASI:One + Chat Protocol** | The whole product runs inside one ASI:One conversation. Every agent speaks the Agent Chat Protocol and publishes its manifest. |
| **Interactive cards** | Every reply is a picture card: a menu banner with service tiles, forms, progress cards with each agent's own art, room and hall carousels, booking links, and receipts. Selections carry stable workflow and option IDs. |
| **Real-time data** | Live LibCal availability, re-checked right before the link. Hall details from the school's own pages. Events from the school's own calendar. IPEDS for school facts. |
| **Error recovery** | A taken room reroutes to the others. No booking system found asks for a link. A LibCal outage falls back to the school's booking link, never invented rooms. Silent agents are retried once. |
| **Safety** | Email local parts are stripped before anything is stored. Fetches are https-only, public-address-only, redirect-vetted, and byte- and time-capped. Page text is evidence, never instructions. |

## 🎨 Cards that look like campus

Every card has a picture. The menu is a banner and a grid of tiles, one per service. Progress
cards show the specialist at work in its own art. Housing halls use the school's own photo of the
building, and wellness events use the event's own photo, with our art as a fallback. The art is
drawn in code (`scripts/make_card_art.py`), served from the repo, and loaded by the student's
browser, so ResourcePilot never fetches an image itself.

## 🛠 How we built it

- **Fetch.ai uAgents:** eight agents, each run by `agents/run.py` with its own `AGENT_ROLE`, seed,
  and port, and all connected to Agentverse with Mailbox.
- **Agent Chat Protocol** from `uagents_core.contrib.protocols`, with prompt ACKs and cards built
  as `TextContent` plus `MetadataContent`.
- **`ResourcePilotTasks` protocol:** our own `TaskRequest`, `TaskResult`, and `AuditEvent` messages
  for agent-to-agent delegation.
- **Postgres:** the shared durable store for workflows, the audit timeline, one-time claims, and
  the cache of public lookups.
- **IPEDS** (via the Urban Institute Education Data API): matches any school domain to a school and
  supplies housing facts and typical costs.
- **LibCal:** live room availability, read only by Campus Rooms.
- **Localist:** public campus events calendars for the wellness card.
- **pypdf** in a worker thread with a time limit: reads the yearly row of a hall's rate PDF.
- **ElevenLabs** voice agents over **Twilio** or a **SIP trunk** for the emergency demo drill.
- **Docker Compose:** Postgres plus eight independent agent containers, with CI running format,
  lint, type-check, and the tests.

The test suite drives the real agent handlers in process, each role loaded as its own module with
its own address. Every live source is served by offline fakes and ElevenLabs is mocked, so no test
touches the network or places a call.

## 🧗 Challenges we ran into

- **Every school hides its booking system somewhere different.** A `<school>.libcal.com` guess
  works for some schools and misses Purdue entirely. We built a three-step search (hand-checked
  list, LibCal probe, a two-hop crawl of the library site) and, when that finds nothing, an honest
  "paste the link" card instead of a guess.
- **Housing pages are wildly inconsistent.** Some schools list halls on one page, some nest them
  under categories, some put prices in a rates table with summer rows mixed in, and some only in a
  PDF. We read each hall's `<main>` content, skip summer and fees tables, read only the yearly row
  of a rate PDF, and never annualize a price that doesn't state its period.
- **Crawling the open web safely.** An agent that fetches pages from any school could be pointed at
  anything. Every fetch checks that the resolved address is public, vets every redirect, caps
  bytes, and runs on a fetch and time budget. Page text is treated as untrusted evidence: we keep
  links and capped fields, never instructions.
- **Fitting the Agentverse Mailbox limit.** Our first build had 13 agents, including demo seller
  shops and an escrow checkout. The Basic plan allows 8 Mailbox agents, and a fake shop isn't a
  real service, so we cut the payments and the shops and rebuilt around eight agents that each do
  real work.
- **Cards with pictures.** The predefined card kinds can't show images, so our forms and the
  emergency review are custom cards, kept under the 64KB payload and eight-level depth limits.
- **Making a phone call safe to demo.** One button press, one call, never retried, to one number
  only our config can set, with every emergency card saying "In a real emergency, call 911."

## 🏆 Accomplishments we're proud of

- **It works at any US school**, from nothing but an email address, not a hand-built list for one
  campus.
- **Real rooms, re-checked live**, with a reroute when someone books your slot first.
- **Every hall a school lists**, matched to your criteria, with a ruled-out hall hidden only when
  its own page rules it out.
- **Eight agents with real jobs**, real message schemas, and one audit trail across all of them.
- **Honest by design:** no invented rooms, no guessed booking systems, no fake reservations, a
  paid hall match that runs in test mode and never pays your school, and a demo call that can
  never reach 911.

## 📚 What we learned

- The hard part of "AI for students" isn't the language model. It's finding the right page on a
  website nobody designed for agents, and proving it's still true.
- Splitting work across agents pays off when each one owns a real responsibility. Putting all room
  reads in Campus Rooms meant the re-check, the outage fallback, and the reroute each had exactly
  one place to live.
- Forms beat free text for actions that matter. Buttons with stable IDs let us promise a re-check
  happens once and a call is placed once.
- Saying "we couldn't find it, can you paste the link?" builds more trust than a confident guess.

## 🚀 What's next for ResourcePilot

- **More booking systems:** read live availability from EMS, 25Live, and Skedda where schools make
  it public, not just LibCal.
- **More campus services:** dining hours, office hours, tutoring centers, and transit.
- **Always-on hosting:** run all eight agents on a managed host with managed Postgres, so the
  profiles answer around the clock.
- **School partnerships:** a school that runs its own Agentverse agent could hand ResourcePilot
  its room and housing data directly, with no crawling at all.

## 🔗 Additional links

- **ASI:One chat session:** [link]
- **Agentverse profiles:** [one link per agent]
- **GitHub:** https://github.com/kevinjylin/resourcepilotla

## Built with

agentverse · asi:one · asyncpg · docker · elevenlabs · fetch.ai · httpx · ipeds · libcal ·
localist · postgresql · pypdf · python · sip · twilio · uagents
