# ResourcePilot Intake

Turns a submitted form into a validated brief for one campus service. A study-room search gets the
day (up to two weeks ahead), the time of day, the length, and the group size. A housing lookup gets
just the school. A school email arrives already reduced to its domain, and Intake keeps only that:
the part before `@` is never stored, sent, or logged. It reports a missing or invalid detail
instead of guessing, so the Orchestrator can show the form again.

ResourcePilot takes requests only from cards and forms, never free text. Intake never contacts a
school or reads a web page.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
