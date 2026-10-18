# Judging checklist

Capture real evidence for each item. Do not claim an item until you have it.

## Required submission

- [ ] Devpost project page.
- [ ] ASI:One Submission Agent completed.
- [ ] Public GitHub repository.
- [ ] Demo video that follows [demo-script.md](demo-script.md).
- [ ] Bonus: the Orchestrator's Agentverse profile URL and a shared ASI:One chat URL.

## Multi-agent collaboration

- [ ] 15 stable addresses and profiles: nine platform agents and six sellers.
- [ ] Each profile has a role README, a Chat manifest, and a working status-card chat.
- [ ] The audit timeline shows one workflow ID across Intake, the Campus Researcher, Discovery,
      Verification, Analysis, Planner, the Checkout Coordinator, and Memory and Audit.
- [ ] A study-room request shows the Researcher's finding (the school and its booking system) and
      a quote from the separate Campus Rooms seller agent.
- [ ] `make logs` shows real `ResourceHold` quote, hold, and commit messages being handled by
      separate seller containers.

## Payment Protocol and monetization

- [ ] The Orchestrator profile shows the Agent Payment Protocol `seller` manifest.
- [ ] The review card shows the booking fee, and the receipt shows seller payouts plus the fee.
- [ ] Mock Stripe is labeled as making no API call and moving no money.
- [ ] Optional: a stable-testnet explorer link for a test FET payment to the escrow wallet.
- [ ] A duplicate commit returns the same receipt and creates no extra ledger entries. This is
      covered by the tests.

## Interactive cards

- [ ] Services menu, the three forms, progress, room and bundle carousels, detail, vetting,
      review, payment, receipt, housing, paste-link, recovery, and refund cards all render.
- [ ] Every reply is a card: free text gets the services menu. `scripts/print_cards.py` validates
      every payload against the documented card fields.
- [ ] Card selections carry stable workflow and bundle IDs. A prose request for a payment or a hold
      is never acted on.

## Reliability and error recovery

- [ ] A live reroute: Simulate outage on Westwood Print Lab, then a hold that reroutes to Gayley
      Copy Center.
- [ ] A failed booking after payment offers retry or refund, and is refunded automatically if you
      wait. This is covered by the tests.
- [ ] A lost specialist task is retried once. This is covered by the tests.

## Real-time data

- [ ] Study rooms are real open rooms read live from the school's LibCal, and each one is
      re-checked before the booking link is shown.
- [ ] Housing facts come from IPEDS, and the office and portal links come from the school's own
      site.
- [ ] Demo-bundle quotes are generated live by each seller agent from its own inventory and
      existing holds. Capacity changes as holds are placed.
- [ ] Be honest: the demo catalogs are not real stores, IPEDS facts are the 2022 survey year, and
      ResourcePilot does not reserve rooms or apply for housing.

## Keeps operating after the event

- [ ] Deployed to an always-on host with managed Postgres. See
      [agentverse-deployment.md](agentverse-deployment.md).
- [ ] The profiles answer while your laptop is off.

## Repository

- [ ] `make check`, `make build`, and `make verify` pass, and CI is green.
- [ ] Git contains no `.env`, `.profiles.json`, logs, or database data.
- [ ] The Devpost text contains only claims backed by the code and the evidence above.
