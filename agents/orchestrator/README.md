# ResourcePilot Orchestrator

The ASI:One entrypoint for ResourcePilot: campus services for a student's first weeks, at any US
school. Everything runs on buttons and short forms.

- **Study rooms.** Enter your school email and a time. The Campus Researcher finds your library's
  room system, and Campus Rooms shows real open rooms. ResourcePilot re-checks the room you pick
  and hands you the school's booking link; you book with your own sign-in. It is always free.
- **Health & wellness.** Enter your school email and a time. Campus Rooms shows open meditation,
  reflection, sensory, and other wellness rooms from your school's LibCal, and the Campus
  Researcher lists the week's health and wellness events with their RSVP links. Every wellness card
  starts with 988 and 911. ResourcePilot never asks about your health or books appointments.
- **Campus housing.** IPEDS dorm beds, typical room and board, and meal plans, plus your housing
  office and application portal, re-checked live. You apply on the school's site.
- **Project bundle.** A labeled demo: a camera, a podcast studio, and posters from five demo shops
  near UCLA. It is held all at once before you pay, and you can add a free study room at your
  school. Payment is a Mock Stripe checkout that makes no Stripe API call and moves no money, or
  faucet-issued Fetch stable-testnet FET.
- **My bookings.**

It owns the workflow, the interactive cards, and escrow payment. It delegates every step to
separately addressed agents: Intake, Campus Researcher, Seller Discovery, Seller Verification,
Logistics Analysis, Bundle Planner, Checkout Coordinator, and Memory and Audit. Nothing is charged
until every paid seller has confirmed a hold, and sellers are paid only after every booking
commits. If a booking fails after payment, the payment stays in escrow for a retry and is refunded
automatically if you do not retry.

Protocols: Agent Chat Protocol, ResourcePilotTasks, Agent Payment Protocol (`seller`).
