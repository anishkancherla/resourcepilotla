# ResourcePilot Planner

Ranks the verified open study rooms into up to six options: earliest opening first, then the
snuggest fit for the group. The first option is labeled "Earliest opening". Each option has a
stable ID made from the room and its start time, so a button press always names the same room and
slot.

When a re-check finds a room taken, the Orchestrator asks the Planner again with that room
excluded. Only that room is dropped, and the student sees the other open rooms. If none is left,
it says so.

It never reads a school's pages, never checks availability itself, and never books anything.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
