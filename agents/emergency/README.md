# ResourcePilot Emergency Dispatch

<img src="https://raw.githubusercontent.com/kevinjylin/resourcepilotla/main/assets/avatars/emergency.jpg" alt="ResourcePilot Emergency Dispatch" width="160">

Runs the emergency **demo drill**. A student picks **Emergency (demo)** in the ResourcePilot
Orchestrator's menu and fills in a short form: what kind of emergency (medical, fire or smoke,
someone in danger, or other), where on campus, how many people need help, optional details,
whether they are safe, and an optional school name.

1. **Preview.** The Orchestrator sends this agent a `phase="preview"` task. It validates the form,
   removes email addresses, caps free text, and returns the review data. The review card shows the
   masked demo number ("•••• 1234"), "not 911", the voice's opening line, and the facts, with
   **Call now** and **Cancel**.
2. **Call.** **Call now** is claimed once per workflow by the Orchestrator, which then sends a
   `phase="call"` task. This agent also claims the call once per workflow and asks an ElevenLabs
   voice agent to phone one pre-configured demo contact (a teammate playing the dispatcher) over
   Twilio or a SIP trunk (such as Telnyx). The voice plays the caller: it speaks for the student,
   reports what happened, where, how many people need help, and whether they are safe, answers
   the dispatcher's questions from the form only, and hangs up once the dispatcher has what they
   need. Put that phone on speaker for the demo.
3. **Check the call.** A `phase="status"` task reads the call's status, duration, summary, and
   transcript from ElevenLabs, and the Orchestrator shows them on a card.

It never calls 911 or any real emergency service:

- It only dials the number in `EMERGENCY_DEMO_TO_NUMBER`. Nothing typed in chat, a form, or a
  task can choose or change the number.
- It refuses emergency, crisis, and service numbers (911, 112, 999, 988, N11 codes, and known
  crisis hotlines), and anything that is not a full phone number with a country code.
- The voice always opens with "this is a ResourcePilot demo drill, not a real emergency".
- Each workflow places at most one call and never retries, even after a timeout, so one button
  press can't dial twice. A call starts only from the Call now button, never from prose.
- Free text is length-capped and stripped of email addresses before it is sent. Cards show only
  the last four digits of the demo number.
- Errors never include the API key, a phone number, or raw response bodies. When ElevenLabs
  refuses a call, the card shows its short reason with every phone number removed.
- It is the only ResourcePilot agent that talks to ElevenLabs (`api.elevenlabs.io` or
  `api.<region>.elevenlabs.io`, over https).
- Every emergency card says it is a demo drill that does not contact 911 or campus police, and
  "In a real emergency, call 911."

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.

## Setup (do this yourself, about 15 minutes)

1. **Phone number.** Pick one:
   - **Twilio** (`ELEVENLABS_TELEPHONY=twilio`): an upgraded Twilio account. A Twilio *trial*
     can't be used: its call API accepts only Twilio's sample call instructions, so ElevenLabs
     gets "trial accounts have limited parameter access". Import a purchased Twilio number, or a
     verified caller ID for outbound-only calls.
   - **Telnyx SIP trunk** (`ELEVENLABS_TELEPHONY=sip_trunk`): a Telnyx trial's $5 testing
     credit covers one local number. The trial can dial only its one verified number, so verify
     your teammate's phone. In Telnyx, create an FQDN SIP connection with outbound credentials,
     add the FQDN `sip.rtc.elevenlabs.io` (TCP), set an outbound voice profile, and assign the
     number to the connection. See ElevenLabs' Telnyx guide for the current fields.
2. **ElevenLabs → Agents → Phone Numbers:** import the number ("From Twilio", or "From SIP Trunk"
   with outbound address `sip.telnyx.com`, TCP, and the Telnyx connection's username and
   password). Copy its phone number ID.
3. **ElevenLabs → Agents → create an agent:**
   - Pick a calm, clear voice.
   - Under **Security**, turn on the overrides for **System prompt** and **First message**.
     ResourcePilot sends both with every call: the opening line always starts with the demo
     notice, and the prompt casts the voice as the caller asking for help, built from the
     student's form. Whatever prompt you type in the dashboard is replaced for these calls.
   - Under **Tools → System tools**, make sure **End call** is on (agents made in the dashboard
     have it by default; agents made through the API don't). Without it the voice can't hang
     up, and the call stays open until the other side hangs up or the time limit runs out.
   - Keep conversations short: set the maximum duration to about 3 minutes, and a silence
     timeout so a quiet line ends on its own.
   - The dashboard's system prompt can be anything short, since it's replaced on each call.

4. **Add these to `.env`** (never commit it). The Dispatcher's own `EMERGENCY_SEED`,
   `EMERGENCY_ADDRESS`, and `EMERGENCY_PORT` (8007) come from bootstrap:

   | Variable | Value |
   |---|---|
   | `ELEVENLABS_API_KEY` | An ElevenLabs API key with Agents access |
   | `ELEVENLABS_AGENT_ID` | The agent's ID |
   | `ELEVENLABS_PHONE_NUMBER_ID` | The imported number's ID (`phnum_...`) |
   | `EMERGENCY_DEMO_TO_NUMBER` | Your teammate's phone, e.g. `+13105550142` |
   | `ELEVENLABS_TELEPHONY` | `twilio` (default) or `sip_trunk`, matching how you imported it |
   | `ELEVENLABS_API_BASE` | Optional; a regional `https://api.<region>.elevenlabs.io` host |

5. **Existing `.env`?** After adding the settings, run `make bootstrap-missing`. It adds this
   agent's identity without changing any other seed. If port 8007 is already used by another
   agent in your `.env` (older layouts used 8007 for Memory and Audit), it picks the next free
   port. Then run `make profiles` and connect this agent's Mailbox.

ElevenLabs and your phone provider bill each call to your accounts. The tests mock the ElevenLabs API, so no
test places a real call; a live call needs this setup.
