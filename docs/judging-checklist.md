# Judging checklist

Capture real evidence for each item. Do not claim an item until you have it.

## Required submission

- [ ] Devpost project page.
- [ ] ASI:One Submission Agent completed.
- [ ] Public GitHub repository.
- [ ] Demo video that follows [demo-script.md](demo-script.md).
- [ ] Bonus: the Orchestrator's Agentverse profile URL and a shared ASI:One chat URL.

## Multi-agent collaboration

- [ ] 7 stable addresses and profiles, all connected with Mailbox: Orchestrator, Intake, Campus
      Researcher, Campus Rooms, Verification, Planner, and Memory and Audit.
- [ ] Each profile has a role README, Chat and Tasks manifests, and a working status-card chat.
- [ ] The audit timeline shows one workflow ID across Intake, the Campus Researcher, Campus
      Rooms, Verification, the Planner, the re-check, and the booking.
- [ ] A study-room request shows the Researcher's finding (the school and its booking system) and
      real rooms read by the separate Campus Rooms agent.
- [ ] `make logs` shows each step handled by a separate agent container.

## Interactive cards

- [ ] Services menu, the rooms and housing forms, progress, rooms carousel, room detail, booking
      link, paste-link, housing, My bookings, error, and status cards all render.
- [ ] Every reply is a card: free text gets the services menu. `scripts/print_cards.py` validates
      every payload against the documented card fields.
- [ ] Card selections carry stable workflow and option IDs. A prose request for a re-check is
      never acted on, and a double click re-checks only once. Both are covered by the tests.

## Reliability and error recovery

- [ ] A room taken during the re-check reroutes to the other open rooms, live or in
      `tests/test_network.py`.
- [ ] A school with no recognized booking system gets the paste-link card, and the pasted link is
      checked with the same rules.
- [ ] A silent specialist is retried once with a fresh request ID, then fails with a clear card.
- [ ] A silent re-check returns to the rooms carousel. This is covered by the tests.
- [ ] A LibCal outage falls back to the school's booking link, never invented rooms. This is
      covered by the tests.

## Real-time data

- [ ] Study rooms are real open rooms read live from the school's LibCal, and each one is
      re-checked before the booking link is shown.
- [ ] Housing facts come from IPEDS, and the office and portal links come from the school's own
      site and are re-opened by Verification.
- [ ] Be honest: IPEDS facts are the 2022 survey year, the LibCal availability endpoint is not a
      documented API, and ResourcePilot takes no payments, does not reserve rooms, and does not
      apply for housing.

## Keeps operating after the event

- [ ] Deployed to an always-on host with managed Postgres and outbound https. See
      [agentverse-deployment.md](agentverse-deployment.md).
- [ ] The profiles answer while your laptop is off.

## Repository

- [ ] `make check`, `make build`, and `make verify` pass, and CI is green.
- [ ] Git contains no `.env`, `.profiles.json`, logs, or database data.
- [ ] The Devpost text contains only claims backed by the code and the evidence above.
