# ResourcePilot Seller Verification

Decides which quotes ResourcePilot is willing to show a student. Each quote must pass:

- identity: the sender matches the registry and the listing is verified;
- protocol conformance: version, currency, hold window, listed categories, and price arithmetic;
- track record: failure rate and a new-seller spending cap;
- policy: availability and eligibility, such as gear training for DSLR kits.

Campus Rooms may offer only free handoff lines whose booking link is on the school's own site or a
known booking system.

For a housing lookup it independently re-opens every official link the Campus Researcher found and
drops any that no longer open on the school's site. For wellness it re-fetches the school's events
feed itself and drops any event the feed no longer lists. A wellness search with no room to offer
is not a failure: Verification passes on the reason so the card can say why.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.
