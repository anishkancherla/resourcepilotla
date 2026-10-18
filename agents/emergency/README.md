# ResourcePilot Emergency Dispatcher

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
   Twilio and describe the emergency out loud. Put that phone on speaker for the demo.
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
- Errors never include the API key, the full number, or raw response bodies.
- It is the only ResourcePilot agent that talks to ElevenLabs (`api.elevenlabs.io` or
  `api.<region>.elevenlabs.io`, over https).
- Every emergency card says it is a demo drill that does not contact 911 or campus police, and
  "In a real emergency, call 911."

Its profile chat shows its role and points to the ResourcePilot Orchestrator.

Protocols: Agent Chat Protocol, ResourcePilotTasks.

## Setup (do this yourself, about 15 minutes)

1. **Twilio:** get a phone number. On a trial account, verify your teammate's number under
   Verified Caller IDs, because trial accounts can only call verified numbers.
2. **ElevenLabs → Agents → Phone Numbers:** import the Twilio number. Copy its phone number ID.
3. **ElevenLabs → Agents → create an agent:**
   - Pick a calm, clear voice.
   - Under **Security**, turn on the override for **First message**. ResourcePilot sets the
     opening line so it always starts with the demo notice.
   - Keep conversations short (a maximum duration of about 3 minutes).
   - System prompt:

     ```text
     You are ResourcePilot's automated emergency reporting assistant, calling a dispatcher
     during a demo drill for a student at {{school}}. {{demo_notice}}
     Report the incident clearly and calmly:
     - What: {{emergency_type}}
     - Where: {{location}}
     - People who need help: {{people}}
     - Details from the student: {{details}}
     - The student says they are safe right now: {{caller_safe}}
     Answer the dispatcher's questions using only these facts. If you don't know something,
     say so. Never invent names, injuries, or addresses. Keep each answer to one or two
     sentences. When the dispatcher has what they need, thank them and end the call.
     ```

4. **Add these to `.env`** (never commit it). The Dispatcher's own `EMERGENCY_SEED`,
   `EMERGENCY_ADDRESS`, and `EMERGENCY_PORT` (8007) come from bootstrap:

   | Variable | Value |
   |---|---|
   | `ELEVENLABS_API_KEY` | An ElevenLabs API key with Agents access |
   | `ELEVENLABS_AGENT_ID` | The agent's ID |
   | `ELEVENLABS_PHONE_NUMBER_ID` | The imported Twilio number's ID |
   | `EMERGENCY_DEMO_TO_NUMBER` | Your teammate's phone, e.g. `+13105550142` |
   | `ELEVENLABS_API_BASE` | Optional; a regional `https://api.<region>.elevenlabs.io` host |

5. **Existing `.env`?** After adding the settings, run `make bootstrap-missing`. It adds this
   agent's identity without changing any other seed. If port 8007 is already used by another
   agent in your `.env` (older layouts used 8007 for Memory and Audit), it picks the next free
   port. Then run `make profiles` and connect this agent's Mailbox.

ElevenLabs and Twilio bill each call to your accounts. The tests mock the ElevenLabs API, so no
test places a real call; a live call needs this setup.
