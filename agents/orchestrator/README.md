# ResourcePilot Orchestrator

The ASI:One entrypoint for ResourcePilot LA. Tell it what you need in one sentence, for example "a
DSLR, a podcast room, and 20 color posters Friday 3-6 PM under $35", and it books a bundle across
several independent seller agents.

It owns the workflow, the interactive cards, and escrow payment. It delegates every step to
separately addressed agents: Intake, Seller Discovery, Seller Verification, Logistics Analysis,
Bundle Planner, Checkout Coordinator, and Memory and Audit. Nothing is charged until every seller
has confirmed a hold, and sellers are paid only after every booking commits. If a seller fails
to hold, the Bundle Planner reroutes around it. If a booking fails after payment, the payment stays
in escrow for a retry and is refunded automatically if you do not retry.

The sellers are demo businesses with illustrative catalogs. Payment uses either a Mock Stripe
checkout that makes no Stripe API call and moves no money, or faucet-issued Fetch stable-testnet
FET.

Protocols: Agent Chat Protocol, ResourcePilotTasks, Agent Payment Protocol (`seller`).
