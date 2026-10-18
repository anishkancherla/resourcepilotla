# ResourcePilot Seller Discovery

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/discovery.jpg" alt="ResourcePilot Seller Discovery" width="160">

Finds seller agents for a brief and collects quotes from them over the ResourceHold protocol. It
asks every seller in ResourcePilot's registry that sells a requested category, including Campus
Rooms for study rooms. It can optionally search the Almanac for other agents that publish the
ResourceHold seller protocol. Only the live Campus Rooms seller receives the school context
the Campus Researcher found; demo sellers never see it. Quotes are keyed by the authenticated
sender address, and sellers that stay silent past the quote window are reported, not waited on.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks, ResourceHold (`buyer`).
