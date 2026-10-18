# Card art and prompts

Every ASI:One card shows one of these images. `scripts/make_card_art.py` draws them as flat
illustrations (SVG sources in `assets/src`, JPEGs in `assets/cards` and `assets/avatars`).

To use richer art, such as images from an image model in the style of the ASI:One Welcome Week
hubs, generate each image below and save it as a JPEG under the same name. Cards pick images by
name, so no code changes. Push to `main` (or set `CARD_IMAGE_BASE`) for ASI:One to load them.

## Shared style

> Polished 3D illustration, glossy app-icon style, soft studio lighting, rich gradient sky with
> faint stars, one rounded glass tile in the center holding the subject, two small glass bubbles
> with related objects at the sides, friendly and modern, no text, no logos, no real people's
> faces. 16:9, 1200x675 for cards; square 512x512 for avatars.

## Cards (`assets/cards/<name>.jpg`)

| Name | Subject |
| --- | --- |
| `cat-camera` | A rental camera on a soft gradient, a tripod and a microphone floating nearby |
| `cat-microphone` | A USB podcast microphone on a stand, warm stage light |
| `cat-podcast-room` | A podcast studio: headphones and a microphone on a desk, purple light |
| `cat-posters` | Two bright printed posters fanned out, sunset colors |
| `cat-tripod` | A camera on a tripod, night studio light |
| `choice-dslr` | A DSLR camera on a warm gradient |
| `choice-mirrorless` | A compact mirrorless camera on a blue gradient |
| `choice-none` | A simple 'none' circle-slash icon on a night gradient |
| `choice-podcast` | Podcast headphones and a microphone |
| `choice-study` | A study table with a lamp |
| `event-default` | A campus wellness event: confetti, a calendar, and a ticket, magenta and pink light |
| `hero-holds` | A golden padlock holding several items safely, cool blue light |
| `hero-info` | A friendly information badge, calm blue light |
| `hero-link` | Chain links and a magnifying glass searching for a booking link |
| `hero-payment` | Two payment cards and a coin, green light; clearly a simulation, no real brand |
| `hero-receipt` | A paper receipt with a green check mark |
| `hero-recovery` | Circular retry arrows around a shield, warm orange light |
| `hero-refund` | A gold coin with an arrow returning it, green light |
| `hero-vetting` | A shield with a check mark, a padlock and an info badge nearby, blue light |
| `hero-warning` | A warning triangle with retry arrows, orange light |
| `menu-banner` | A friendly US college campus at dusk: a clock-tower hall, a glowing lotus, and a cozy dorm, with floating bubbles holding a camera and a calendar |
| `role-analysis` | A friendly round white robot agent measuring time and walking distance |
| `role-discovery` | A friendly round white robot agent collecting quotes from several shops |
| `role-fulfillment` | A friendly round white robot agent holding items with a padlock and confirming bookings |
| `role-intake` | A friendly round white robot agent reading a form and a calendar |
| `role-memory-audit` | A friendly round white robot agent keeping receipts and a timeline |
| `role-orchestrator` | A friendly round white robot agent the conductor that runs the whole chat, holding a campus |
| `role-planner` | A friendly round white robot agent arranging items into bundles |
| `role-researcher` | A friendly round white robot agent researching a school's website with a magnifying glass |
| `role-verification` | A friendly round white robot agent checking sellers with a shield |
| `room-study-1` | A small group study room with a whiteboard, table, and lamp, indigo evening light |
| `room-study-2` | A cozy study booth with a lamp and stacked books, night sky through the window |
| `room-study-3` | A bright collaborative study room with a screen and chairs, blue daylight |
| `room-wellness` | A serene campus meditation room with cushions, a lotus, and soft teal light |
| `seller-campus-rooms` | A small storefront: a campus study-room desk with a gold awning |
| `seller-flash-prints` | A small storefront: a cheap print shop with a grey awning and a warning sign (unverified) |
| `seller-gayley-copy` | A small storefront: a copy center with a blue awning |
| `seller-gear-share` | A small storefront: a camera and gear rental shop with a mint awning |
| `seller-studio-rooms` | A small storefront: a podcast studio rental with a lilac awning |
| `seller-westwood-print` | A small storefront: a poster print shop with a coral awning |
| `svc-bookings` | A tidy calendar with checked-off bookings and receipt cards, cool blue light |
| `svc-bundle` | A creative project kit: a camera, a podcast microphone, and colorful posters, with a shield and a payment card floating nearby |
| `svc-housing` | A welcoming campus residence hall with lit windows and a warm sunset, a key and a calendar floating nearby |
| `svc-rooms` | A quiet library study room: a wooden table, a laptop, a warm desk lamp, books on a shelf, a big window |
| `svc-wellness` | A calm meditation corner with a glowing pink lotus, soft teal light, plants, and a small calendar of wellness events |
| `wellness-hero` | Campus wellness: a lotus, a calendar with a highlighted day, and a protective shield, teal and mint light |

## Agent avatars (`assets/avatars/<key>.jpg`)

Square versions of the `role-*` and `seller-*` cards above, one per agent identity key (for
example `memory_audit.jpg`). Upload each one as the agent's avatar on its Agentverse profile;
that step is tied to your account.
