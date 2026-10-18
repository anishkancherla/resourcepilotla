# ResourcePilot Checkout Coordinator

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/fulfillment.jpg" alt="ResourcePilot Checkout Coordinator" width="160">

Runs all-or-nothing checkout across sellers. It places a time-limited hold with every seller in a
bundle, and if any seller rejects or stays silent it releases the holds that did succeed. After the
buyer's escrow payment is verified, it commits every hold and reports each seller's confirmation
code; if a commit fails, it releases the rest so nothing is half-booked.

A free Campus Rooms line is held for its own time slot, which makes the seller re-check that the
room is still open. A bundle made only of free rooms commits with a `free:` reference instead of a
payment, and only $0 Campus Rooms handoffs may use that reference.

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks, ResourceHold (`buyer`).
