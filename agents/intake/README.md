# ResourcePilot Intake

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/intake.jpg" alt="ResourcePilot Intake" width="160">

Turns a submitted form into a validated brief for one campus service. A study-room search gets the
day (up to two weeks ahead), the time of day, the length, and the group size. A housing lookup gets
just the school. The student sets their school email once; the Orchestrator reduces it to its
domain and adds that to every form, and Intake keeps only that:
the part before `@` is never stored, sent, or logged. It reports a missing or invalid detail
instead of guessing, so the Orchestrator can show the form again.

ResourcePilot takes requests only from cards and forms, never free text. Intake never contacts a
school or reads a web page.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
