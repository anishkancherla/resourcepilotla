# ResourcePilot Orchestrator

The ASI:One entrypoint for ResourcePilot: campus help for a student's first weeks, at any US
school. Everything runs on buttons and short forms, and every reply is a card.

- **Study rooms.** Enter your school email and a time. The Campus Researcher finds your library's
  room system, and Campus Rooms shows real open rooms. ResourcePilot re-checks the room you pick
  and hands you the school's booking link; you book with your own sign-in. It is always free.
- **Health & wellness.** Enter your school email and a time. Campus Rooms shows open meditation,
  reflection, sensory, and other wellness rooms from your school's LibCal, and the Campus
  Researcher lists the week's health and wellness events with their RSVP links. Every wellness card
  starts with 988 and 911. ResourcePilot never asks about your health or books appointments.
- **Campus housing.** IPEDS dorm beds, typical room and board, and meal plans, plus your housing
  office and application portal, each link re-checked. You apply on the school's site.
- **My bookings.** Your recent re-checked rooms and their booking links.

It owns the chat, the services menu, the cards, and the workflow. It delegates every step with
`ctx.send` to separately addressed agents: Intake, Campus Researcher, Campus Rooms, Verification,
Planner, and Memory and Audit. It accepts only results from the agent it asked, for the request it
is waiting on, and it retries a silent step once. If a room is taken when it is re-checked, the
Planner shows you the other open rooms.

It never takes payment, never reserves a room, and never applies for housing. Free text gets the
services menu; it never starts a request from prose.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
