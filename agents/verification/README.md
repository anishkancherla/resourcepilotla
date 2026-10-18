# ResourcePilot Verification

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/verification.jpg" alt="ResourcePilot Verification" width="160">

Decides which results ResourcePilot is willing to show a student, before the student sees them.

For a study-room search it checks every room offer from Campus Rooms. An offer passes only if:

- it names a room and has a stable room ID;
- it is available (an unavailable offer keeps its own reason, such as no room open for the group);
- its booking link is https on the school's own site or a known booking system;
- its live time slot, when it has one, is inside the window the student asked for;
- its seat count, when known, fits the whole group.

Each rejection is kept with its reason. If nothing passes, the student gets a card that says why.

For a housing lookup it independently re-opens every official link and hall page the Campus
Researcher found and drops any that no longer open on the school's site. For wellness it checks Campus Rooms'
wellness-room offers the same way, then re-fetches the school's events feed itself and drops any
event the feed no longer lists. A wellness search with no room to offer is not a failure:
Verification passes on the reason so the card can say why.

It never changes a room's details, never ranks rooms, and never books anything.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
