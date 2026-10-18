# Card art and prompts

Every ASI:One card shows one of these images. `scripts/make_card_art.py` draws them as flat
illustrations (SVG sources in `assets/src`, JPEGs in `assets/cards` and `assets/avatars`).
Run `uv run python scripts/make_card_art.py` to redraw them; it draws exactly the names below and
removes any other art under `assets`.

To use richer art, such as images from an image model, generate each image below and save it as a
JPEG under the same name. Cards pick images by name, so no code changes. Push to `main` (or set
`CARD_IMAGE_BASE` to another https folder) for ASI:One to load them. With `CARD_IMAGE_BASE` empty,
cards show no images and stay valid.

## Shared style

> Polished 3D illustration, glossy app-icon style, soft studio lighting, rich gradient sky with
> faint stars, one rounded glass tile in the center holding the subject, two small glass bubbles
> with related objects at the sides, friendly and modern, no text, no logos, no real people's
> faces. 16:9, 1200x675 for cards; square 512x512 for avatars.

## Cards (`assets/cards/<name>.jpg`)

| Name | Subject |
| --- | --- |
| `menu-banner` | A friendly US college campus at dusk: a clock-tower hall, a glowing lotus, and a cozy dorm, with floating bubbles holding a calendar and a phone |
| `svc-rooms` | A quiet library study room: a wooden table, a laptop, a warm desk lamp, books on a shelf, a big window |
| `svc-wellness` | A calm meditation corner with a glowing pink lotus, soft teal light, and a small calendar of wellness events |
| `svc-housing` | A welcoming campus residence with lit windows and a warm sunset, a campus hall and a calendar floating nearby |
| `svc-emergency` | A smartphone showing a practice call with a gold drill badge, calm blue light, an info badge and a shield nearby; no police, sirens, or ambulances |
| `svc-bookings` | A tidy calendar with checked-off bookings and receipt cards, cool blue light |
| `room-study-1` | A small group study room with a whiteboard, table, and lamp, indigo evening light |
| `room-study-2` | A cozy study booth with a lamp and stacked books, night sky through the window |
| `room-study-3` | A bright collaborative study room with a screen and chairs, blue daylight |
| `room-wellness` | A serene campus meditation room with cushions, a lotus, and soft teal light |
| `wellness-hero` | Campus wellness: a lotus, a calendar with a highlighted day, and a protective shield, teal and mint light |
| `event-default` | A campus wellness event: confetti, a calendar, and a ticket, magenta and pink light |
| `hall-default` | A four-story brick campus residence hall with lit windows, an entry canopy, and two trees at sunset (shown when a hall's page has no photo) |
| `hero-info` | A friendly information badge, calm blue light |
| `hero-warning` | A warning triangle with a shield and an info badge, orange light |
| `hero-link` | Chain links and a magnifying glass searching for a booking link |
| `hero-receipt` | A paper handoff record with a green check mark (a booking link handed over, never a payment) |
| `hero-call` | A calm demo-drill phone call: a ringing smartphone with a gold practice badge, transcript speech bubbles, and an info badge, soft blue light; no police, sirens, or ambulances |
| `role-orchestrator` | A friendly round white robot agent, the conductor that runs the whole chat, holding a campus |
| `role-intake` | A friendly round white robot agent reading a form and a calendar |
| `role-researcher` | A friendly round white robot agent researching a school's website with a magnifying glass |
| `role-campus-rooms` | A friendly round white robot agent reading open study and wellness rooms, a study desk and a lotus nearby |
| `role-verification` | A friendly round white robot agent re-checking links and rooms with a shield |
| `role-planner` | A friendly round white robot agent ranking open rooms and matching residence halls |
| `role-memory-audit` | A friendly round white robot agent keeping receipts and a timeline |
| `role-emergency` | A friendly round white robot agent holding a phone with a practice badge, placing a demo-drill call |

## Agent avatars (`assets/avatars/<key>.jpg`)

Square versions of the `role-*` cards above, one per agent identity key: `orchestrator`,
`intake`, `researcher`, `campus_rooms`, `verification`, `planner`, `memory_audit`, and
`emergency` (for example `memory_audit.jpg`). Upload each one as the agent's avatar on its
Agentverse profile; that step is tied to your account.
