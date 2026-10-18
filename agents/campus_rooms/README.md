# ResourcePilot Campus Rooms

Reads real open library study rooms at the student's own school, at any school whose room system
the Campus Researcher found. It is the only ResourcePilot agent that reads room availability.

For a health and wellness search it reads only wellness rooms: LibCal spaces named for
meditation, reflection, prayer, wellness, lactation, rest, or sensory needs. They are never
offered as study rooms. When a school lists none, has no LibCal, or has none open, it says so
instead of offering a link. It never asks why the student wants the room.

It answers two kinds of tasks from the Orchestrator:

- **Find rooms.** For LibCal schools it reads the public room list and the open slots from the
  school's own LibCal pages, keeps study spaces that fit the group, and offers each room's first
  opening inside the requested window. For other systems (EMS, 25Live, Skedda, Robin, or a booking
  page) it offers the school's booking page, because that availability is not public. If LibCal's
  live times fail, it offers the booking page instead. If no room fits, it says so.
- **Re-check one room.** Right before the student gets the booking link, it reads that exact slot
  again, uncached, and reports whether it is still open. If it was taken, the Planner shows the
  other open rooms.

It reads only a LibCal host on the school's own domain or `libcal.com`, and only over https to
public addresses. It never invents rooms, never reserves a room, and never takes payment. Every
room is free, and the student books on the school's site with their own sign-in.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
