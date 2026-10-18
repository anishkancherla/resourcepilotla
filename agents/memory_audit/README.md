# ResourcePilot Memory and Audit

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/memory_audit.jpg" alt="ResourcePilot Memory and Audit" width="160">

Keeps the append-only audit timeline for every workflow: when it started, each specialist's
result, retries and timeouts, each re-check and reroute, and each booking link handed over. It
accepts events only from ResourcePilot's own agents and records each event once. The rooms under
**My bookings** are the same workflows' re-checked rooms, kept in the shared Postgres store.

It never stores an email address (only the school domain ever reaches a workflow), never edits or
deletes an event, and never takes part in a booking.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
