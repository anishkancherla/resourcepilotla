# Campus Rooms (live library rooms)

An independent seller agent that quotes real open library study rooms at the student's own school,
at any school whose room system the Campus Researcher found. For LibCal schools it reads the public
room list and open slots. For other systems it offers the school's booking page, because that
availability is not public.

Every Campus Rooms offer is a free handoff. When asked to hold, it re-checks the slot live. When
asked to commit, it hands over the school's booking link. It never reserves a room, never takes
payment, and is never paid out. The student books with their own sign-in on the school's site.

Protocols: Agent Chat Protocol, ResourceHold 1.1.0 (`seller`).
