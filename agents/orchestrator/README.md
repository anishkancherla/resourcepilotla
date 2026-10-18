# ResourcePilot Orchestrator

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/orchestrator.jpg" alt="ResourcePilot Orchestrator" width="160">

The ASI:One entrypoint for ResourcePilot: campus help for a student's first weeks, at any US
school. Everything runs on buttons and short forms, and every reply is a card. The first card
asks for your school email once; only its domain is kept, and every service uses it. **Change
school** on the menu switches it.

- **Study rooms.** Pick a time. The Campus Researcher finds your library's
  room system, and Campus Rooms shows real open rooms. ResourcePilot re-checks the room you pick
  and hands you the school's booking link; you book with your own sign-in. It is always free.
- **Health & wellness.** Pick a time. Campus Rooms shows open meditation,
  reflection, sensory, and other wellness rooms from your school's LibCal, and the Campus
  Researcher lists the week's health and wellness events with their RSVP links. Every wellness card
  starts with 988 and 911. ResourcePilot never asks about your health or books appointments.
- **Campus housing.** Say who you are, the style you want, your must-haves, and a budget. You get
  every on-campus hall your school lists, matched to them, each with its photo, style, features,
  and listed price, plus IPEDS facts and the apply link. You apply on the school's site. The
  facts are free; when payments are set up, the hall-by-hall match is unlocked with a Stripe
  test-mode card or testnet FET, and the Orchestrator checks the payment itself before the
  Planner starts.
- **Emergency (demo).** A demo drill, never a real emergency call. Say what is happening, where,
  and how many people need help, then review the call. **Call now** has the Emergency Dispatcher
  phone your team's demo contact, never 911, and **Check the call** shows the summary and
  transcript. In a real emergency, call 911.
- **My bookings.** Your recent re-checked rooms and their booking links.

It owns the chat, the services menu, the cards, and the workflow. It delegates every step with
`ctx.send` to separately addressed agents: Intake, Campus Researcher, Campus Rooms, Verification,
Planner, Memory and Audit, and Emergency Dispatcher. It accepts only results from the agent it
asked, for the request it is waiting on, and it retries a silent step once. The demo call is the
exception: it is never retried. If a room is taken when it is re-checked, the Planner shows you
the other open rooms.

Its only payment is that optional hall match, in test mode only. It never reserves a room, never
applies for or pays for housing, and never calls 911.
Free text gets the services menu; it never starts a request or a call from prose.

Protocols: Agent Chat Protocol, ResourcePilotTasks, Agent Payment Protocol (seller).
