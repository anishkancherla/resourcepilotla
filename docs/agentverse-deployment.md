# Agentverse connection and deployment

## Local Inspector and Mailbox (testing)

1. Run `make start`, then `make profiles`.
2. Sign in to [Agentverse](https://agentverse.ai/).
3. Open every Inspector URL that `make profiles` printed: all eight agents.
4. Confirm the address, click **Connect**, and choose **Mailbox**.
5. On each profile, confirm that the README and the Chat Protocol and `ResourcePilotTasks`
   manifests are there.
6. Open each profile chat and check that it answers with its status card.

Agents send each other messages through their Mailboxes, so all eight must be connected before a
request can complete. The Agentverse Basic plan allows 8 Mailbox agents, so all eight fit on it
exactly, with none to spare. Check the current plan limits on Agentverse before you add an agent.

Upgrading from the 15-agent version? Keep your `.env`. The agents keep their seeds, so their
addresses and profiles stay the same. `make verify` names the leftover settings for agents that no
longer run. You can disconnect those agents' Mailboxes in Agentverse to free the slots.

Adding the Emergency Dispatcher to an existing `.env`? Add its ElevenLabs and demo-number settings
(see [agents/emergency/README.md](../agents/emergency/README.md)), then run
`make bootstrap-missing`. It adds the Dispatcher's identity without changing any other seed. If
port 8007 is already used by another agent in that `.env` (older layouts used 8007 for Memory and
Audit), it picks the next free port. Then open its Inspector link and connect its Mailbox.

In Docker, every published port is bound to host loopback and targets container port `18000`. A
small TCP relay there forwards to the uAgent through container loopback. uAgents accepts Inspector
`/connect` and `/messages` calls only from local callers, and Docker NAT would otherwise present
your browser as a remote caller. Never expose the relay publicly.

Mailbox relays and queues messages for a local agent. It does not keep a sleeping laptop online.

## A persistent submission

The track rewards agents that keep running after the event. To get there:

- Deploy the Compose stack to an always-on host. Keep the same eight seeds so the addresses do not
  change, and keep the seeds and the ElevenLabs settings in the host's secret manager.
- Allow outbound https. The Campus Researcher, Campus Rooms, and Verification read IPEDS (the
  Urban Institute Education Data API), schools' websites, and LibCal. They need no API keys. The
  Emergency Dispatcher calls the ElevenLabs API and needs `ELEVENLABS_API_KEY`.
- Use managed Postgres with backups, and point `DATABASE_URL` at it.
- Re-open every profile after deploying, then test a chat, a study-room search through the
  re-check, a housing lookup, and **My bookings**.
- Capture the shared ASI:One chat link with the Orchestrator and all eight profile links for
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
- publishing a shared chat
- the ElevenLabs agent, the Twilio or Telnyx number, and the demo contact's phone
