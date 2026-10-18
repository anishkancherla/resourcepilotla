# ResourcePilot Verification

Decides which results ResourcePilot is willing to show a student, before the student sees them.

For a study-room search it checks every room offer from Campus Rooms. An offer passes only if:

- it names a room and has a stable room ID;
- it is available (an unavailable offer keeps its own reason, such as no room open for the group);
- its booking link is https on the school's own site or a known booking system;
- its live time slot, when it has one, is inside the window the student asked for;
- its seat count, when known, fits the whole group.

Each rejection is kept with its reason. If nothing passes, the student gets a card that says why.

For a housing lookup it independently re-opens every official link the Campus Researcher found
and drops any that no longer open on the school's site.

It never changes a room's details, never ranks rooms, and never books anything.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
