# Trust model

Why can the Orchestrator send a student to a room-booking link or a housing portal it did not
build? Because every link it shows was found on the school's own site, checked by a separate
agent before the student sees it, and, for a room, read again live right before it is handed
over. ResourcePilot takes no payments, so there is no money to protect: what it protects is the
student's trust in the link and the privacy of their email address. The emergency demo call adds
one more rule: it must never reach a real emergency line (section 6).

## 1. Where rooms and links come from

- **The school.** The Campus Researcher starts from the domain of the student's email, matches it
  to a US institution in IPEDS, and finds the booking system and housing pages on the school's own
  site or its known vendor. If it finds no booking system, it asks the student for the link and
  checks the pasted link with the same rules. It never guesses.
- **The live room system.** Campus Rooms reads open rooms from the school's public LibCal pages.
  For other systems it offers only the school's booking page, because their availability is not
  public. It never invents rooms, and when LibCal's live times fail it falls back to that link.

## 2. Verification of room offers

Verification checks every room offer from Campus Rooms before the Planner ranks it. An offer is
rejected, with the reason kept alongside the workflow, unless all of these hold:

1. **Complete.** The offer has a room name and a stable room ID.
2. **Available.** The offer is not marked unavailable. When no room fits, Campus Rooms returns an
   unavailable offer with the reason ("No room for 4 at … is open for 60 minutes in that window"),
   and Verification passes that reason on.
3. **Safe link.** The booking link is https on the school's domain or one of its subdomains, or
   on a known booking vendor.
4. **In your window.** When the offer has live times, its slot starts and ends inside the window
   the student asked for.
5. **Fits the group.** When the room's seat count is known, it is at least the group size.

If no offer passes, the workflow fails with the first rejection's reason, or "No open room is left
at your school for that time". The student sees an error card, never an unchecked room.

## 3. The live re-check before the link

A carousel can be minutes old. When the student presses **Re-check this room**, the Orchestrator
asks Campus Rooms to read LibCal's availability for that exact slot again, uncached. Only if the
slot is still open does the student get the booking link. If it was taken, the Planner drops just
that room and shows the other open rooms. A booking-page offer has no live times, so it is handed
over as a link, and the card says it was not checked live.

ResourcePilot never reserves a room. The booking link card says so plainly: the student books on
the school's site with their own sign-in.

## 4. Housing link re-checks

For housing, the Researcher finds the housing office page and an application portal that is a link
on the office's own pages or on its housing vendor (StarRez). Verification then re-opens the office
and portal links itself, under the same fetch rules, and drops any that no longer open. The housing
card names any link it dropped. ResourcePilot never applies for, reserves, or pays for
housing.

The Researcher also reads each hall from the school's own housing pages. A hall page is
untrusted text: only a capped name, a photo that is https on the school's domain, style, who it is
for, room types, listed features, the listed price, and a short "about" line are kept. An "about"
line with an email address, a link, or instruction-like words is skipped. Menus, headers, and
footers are removed first, so site-wide words cannot tag every hall. Verification re-opens every
hall page itself. The Planner rules a hall out only on its own page's words; anything a page does
not say is shown as "not listed", never guessed.

Prices from a rates page or a rate PDF are read the same cautious way. A PDF is an untrusted
document: it is fetched only over https on the school's domain under the same address and
redirect checks, capped at 3MB, must start with `%PDF-`, and is parsed for two pages at most in
a worker thread with a five-second limit. Only dollar amounts on its yearly row are used, and
only the room columns its header names; anything unclear yields no number, just the official
link. The IPEDS fallback is always labeled as the school's typical cost, never as a hall's
rate.

## 5. Reading the public web safely

The Campus Researcher, Verification's housing link and events feed re-checks, Campus Rooms, and
the Emergency Dispatcher are the only parts that use the network. The first three fetch web pages,
under the rules in [resourcepilot/research.py](../resourcepilot/research.py) and
[resourcepilot/http.py](../resourcepilot/http.py). The Emergency Dispatcher talks only to the
ElevenLabs API (section 6). The web page rules:

- **Hosts.** Only `https` on port 443, with no user info in the URL. The host must be the school's
  domain or one of its subdomains, or a known vendor: `libcal.com`, `emscloudservice.com`,
  `25live.collegenet.com`, `skedda.com`, `robinpowered.com`, the StarRez housing portals, and
  Localist calendars (`localist.com`).
- **Addresses.** Before each fetch, every address the host resolves to must be publicly routable.
  Private, loopback, and link-local addresses are refused, so a school page cannot point the agent
  at an internal service.
- **Redirects.** Redirects are never followed automatically. Each hop is checked against the same
  rules, up to four hops.
- **Bounds.** A crawl makes at most 12 fetches in 20 seconds, follows at most three links per page,
  and goes two hops deep. Each response is capped in bytes, and every call has a timeout.
- **Untrusted content.** Page text is evidence only. The Researcher extracts links, link text, and
  booking-system fingerprints; it never follows instructions found on a page. A LibCal site found by
  the `<school>.libcal.com` probe must link back to the school, so another school's site is not
  mistaken for yours. The same holds for a `<school>.localist.com` calendar.
- **Events feeds.** From a Localist feed the Researcher keeps only an event's title (80
  characters, tags and control characters stripped), start time, location (60 characters), and
  its calendar link, which must pass the same host rules. Descriptions are never read or shown.
  These fields are display data and never instructions.
- **Health.** The wellness form asks only for the school email, a day, and a duration. Nothing
  about a student's health is asked, stored, or logged, and ResourcePilot books no health or
  counseling appointments. The 988 and 911 crisis line is static text on every wellness card and
  on the menu, so it shows even when nothing at the school can be found.
- **Hosts from another agent.** Campus Rooms reads only a LibCal host that is on the school's own
  domain or `libcal.com`, even though the Researcher already checked it.
- **Privacy.** Only the school domain of an email address is kept. The local part never reaches a
  workflow, an event, a message, the cache, or a log.

A pasted booking link goes through the same checks, so a student cannot use ResourcePilot to fetch
an arbitrary URL.

## 6. The emergency demo call

The emergency service is a demo drill. It never calls 911, campus police, or any emergency
service. The Emergency Dispatcher asks an ElevenLabs voice agent to phone one demo contact, a
teammate playing the dispatcher, over the user's own Twilio number or SIP trunk. The rules are enforced in
[resourcepilot/emergency.py](../resourcepilot/emergency.py), not in a dashboard:

- **One destination.** The Dispatcher dials only `EMERGENCY_DEMO_TO_NUMBER`. Nothing in a chat
  message, the form, or a task payload can choose or change the number.
- **Refused numbers.** 911, 112, 999, 988, N11 codes, listed crisis hotlines, short numbers, and
  anything that is not a full phone number with a country code are refused, even when configured.
- **Demo notice first.** The voice always opens with "this is a ResourcePilot demo drill, not a
  real emergency", so a wrong number never hears a fake emergency it could mistake for a real one.
- **One call per workflow.** The Orchestrator claims the **Call now** press once, and the
  Dispatcher claims the workflow's call once. A call is never retried, even after a timeout: the
  workflow fails, and the student starts a new demo.
- **Never from prose.** Only the **Call now** selection, with its stable workflow ID, can place a
  call.
- **Bounded text.** Free text is trimmed, length-capped, and stripped of email addresses before it
  is sent. The transcript shown on the card is capped too.
- **Masked number.** Cards show only the last four digits ("•••• 1234").
- **No secrets in errors.** Errors never include the API key, the full number, or raw response
  bodies.
- **One provider host.** Only the Emergency Dispatcher talks to ElevenLabs, over https to
  `api.elevenlabs.io` or `api.<region>.elevenlabs.io`.
- **Clear cards.** Every emergency card says it is a demo drill that does not contact 911 or
  campus police, and "In a real emergency, call 911."

## 7. Messages between agents

- Specialists accept tasks only from the Orchestrator's address, and refuse a task past its
  deadline.
- The Orchestrator accepts a result only from the address of the role it claims to come from, only
  once, and only for the request the workflow currently expects.
- Memory and Audit accepts audit events only from ResourcePilot's own agents.
- Card selections carry stable workflow and option IDs. A re-check or a demo call is never
  started from ambiguous prose.

## Limits of this demo

- ResourcePilot cannot see a school's booking rules (who may book, limits per day), so the student
  books on the school's site, which applies them.
- LibCal's availability endpoint is the one its public booking page calls. It is not a documented
  API and may change.
- Wellness rooms and events come only from LibCal and Localist. A school that lists wellness
  spaces elsewhere, or runs another calendar, shows none, and the keyword filter can include
  events a school tags as wellness but a student would not.
- The crawl recognizes common systems and booking words. A school whose library hides its booking
  system behind a sign-in or JavaScript-only navigation gets the paste-link card.
- IPEDS facts are the 2022 survey year. Prices and capacity change, so check current figures with
  the school.
- The demo call depends on the user's own ElevenLabs and phone-provider setup, and both bill each
  call to the user's accounts. A Twilio trial account can't place ElevenLabs calls at all, and a
  Telnyx trial can call only its one verified number. The tests mock
  ElevenLabs, so no test places a real call, and live calls were not part of the automated tests.
