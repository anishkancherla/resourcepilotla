# ResourcePilot Campus Researcher

Works out where a student's own school books study rooms and runs housing, starting only from the
domain of their school email (the part before `@` is never kept). It matches the domain to a US
institution in IPEDS, then finds the library's room-booking system. It checks a short hand-verified
list first, then probes `<school>.libcal.com`. Failing both, it crawls the library's own site,
following room-booking links and recognizing LibCal, EMS, 25Live, Skedda, and Robin. If it finds
nothing it asks the student for the link instead of guessing, and checks that link the same way.
For housing it finds the housing office and application portal and reads IPEDS dorm beds,
typical room and board, and meal plans.

Every fetch is https only. It is limited to the school's domain or a known booking vendor, refused
for private addresses, re-checked on each redirect, and capped by a small fetch and time budget.
Page content is treated as untrusted evidence.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
