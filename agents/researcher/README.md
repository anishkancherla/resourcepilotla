# ResourcePilot Campus Researcher

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/researcher.jpg" alt="ResourcePilot Campus Researcher" width="160">

Works out where a student's own school books study rooms and runs housing, starting only from the
domain of their school email (the part before `@` is never kept). It matches the domain to a US
institution in IPEDS, then finds the library's room-booking system. It checks a short hand-verified
list first, then probes `<school>.libcal.com`. Failing both, it crawls the library's own site,
following room-booking links and recognizing LibCal, EMS, 25Live, Skedda, and Robin. If it finds
nothing it asks the student for the link instead of guessing, and checks that link the same way.
For housing it finds the housing office and application portal, reads IPEDS dorm beds, typical
room and board, and meal plans, and reads every hall the school lists from its own housing pages:
name, photo, style, who it's for, room types, features, listed price, and a short description. For wellness it finds the room system the same way but
never pauses for a link, and it finds the school's public Localist events calendar on
`events.<domain>`, `calendar.<domain>`, or a `<school>.localist.com` site that links back to the
school, keeping the week's health and wellness events. A school without one is told so; nothing is
guessed. It never asks about a student's health.

Every fetch is https only. It is limited to the school's domain or a known booking vendor, refused
for private addresses, re-checked on each redirect, and capped by a small fetch and time budget.
Page content is treated as untrusted evidence, never as instructions. From an events feed it keeps
only a capped title, start time, location, event link, and photo link (https on the school's
domain or `localist-images.azureedge.net`), shown as data and never acted on. It never fetches an
image; only the student's browser loads it. It never reads room availability, never books
anything, and never guesses a booking system.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
