# Agentverse connection and deployment

## Local Inspector and Mailbox (testing)

1. Run `make start`, then `make profiles`.
2. Sign in to [Agentverse](https://agentverse.ai/).
3. Open every Inspector URL that `make profiles` printed: nine platform agents and six
   sellers.
4. Confirm the address, click **Connect**, and choose **Mailbox**.
5. On each profile, confirm that the README and Chat Protocol manifest are there:
   - The Orchestrator also shows the Agent Payment Protocol (`seller`).
   - Discovery, the Checkout Coordinator, and every seller show `ResourceHold`.
6. Open each profile chat and check that it answers with its status card.

Agents send each other messages through their Mailboxes, so all 15 must be connected before a
booking can complete.

Upgrading from the 13-agent version? Run `make bootstrap-missing`. It asks first, then adds
identities only for the Campus Researcher and Campus Rooms, so every existing address and profile
stays the same. Connect just those two new agents with Mailbox.

In Docker, every published port is bound to host loopback and targets container port `18000`. A
small TCP relay there forwards to the uAgent through container loopback. uAgents accepts Inspector
`/connect` and `/messages` calls only from local callers, and Docker NAT would otherwise present
your browser as a remote caller. Never expose the relay publicly.

Mailbox relays and queues messages for a local agent. It does not keep a sleeping laptop online.

## A persistent submission

The track rewards agents that keep running after the event. To get there:

- Deploy the Compose stack to an always-on host. Keep the same 15 seeds so the addresses do not
  change, and keep the seeds in the host's secret manager.
- The Campus Researcher and Campus Rooms need outbound https to IPEDS, schools' websites, and
  LibCal. They need no API keys.
- Use managed Postgres with backups, and point `DATABASE_URL` at it.
- Re-open every profile after deploying, then test a chat, a full Mock Stripe booking, and a
  reroute.
- Capture the shared ASI:One chat link with the Orchestrator and all 15 profile links for
  Devpost.

Hosted Agentverse agents have different process and state constraints. Do not assume this
multi-process stack runs there unchanged. Port each runtime, test it, and document the path you
actually used.

## Manual steps are intentional

This repository does not automate any of the following, because each needs the owner in the
browser:

- Agentverse sign-in
- profile ownership
- Mailbox approval
- faucet requests or wallet approval
- publishing a shared chat
