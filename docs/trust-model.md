# Trust model

Why can an Orchestrator send your money toward a seller it did not build? Because identity, conduct,
and money are each checked in a different place.

## 1. Where sellers come from

**Curated registry.** [resourcepilot/registry.py](../resourcepilot/registry.py) lists each seller's
ID, display name, categories, location, opening hours, and identity status. The registry stores a
seller's agent address and payout wallet as environment variables derived from the seller's seed.
The registry is the only place a seller's identity is asserted.

**Optional open discovery.** With `ALMANAC_DISCOVERY=true`, Seller Discovery also calls
`ctx.get_agents_by_protocol` with the `ResourceHold` seller digest. It then asks up to five
unlisted agents for quotes. Their quotes reach Verification so the rejection is visible, but an
unlisted seller can never pass the identity check. Discovering a seller is not the same as trusting
it.

## 2. Four vetting layers

Seller Verification runs these checks on every quote, in order. A rejection names the layer and the
reason, and the **Why not others?** card shows them.

1. **Identity.** The quote must arrive from the exact registry address of a listing marked
   identity-verified. Quotes are keyed by the authenticated sender address, so a seller cannot
   claim another seller's ID.
2. **Protocol conformance.** These must all hold:
   - Protocol version 1.1.0 and currency USD.
   - A hold time of at least 120 seconds.
   - Offers only in the seller's listed categories.
   - `subtotal = unit price × quantity × hours`, within one cent.
3. **Track record.** Memory and Audit counts each seller's quotes, silences, held and failed holds,
   and committed and failed bookings. A seller with at least three attempts and a failure rate above
   50% is dropped. A seller with fewer than three committed bookings is "new", and any single line
   it offers above `NEW_SELLER_CAP_CENTS` is rejected.
4. **Policy.** The item must be available for the requested window and quantity. Items that require
   training are dropped unless you said you have it. A Campus Rooms offer must be a $0 handoff line,
   and its booking link must be https on your school's domain or a known booking vendor. A handoff
   offer from any other seller is rejected.

After vetting, Logistics Analysis drops offers that cannot be ready in time. The Bundle Planner
never builds a bundle over your budget.

## 3. Conduct at checkout

Vetting judges what a seller promised. Checkout judges what the seller does:

- A hold confirmed at a different price than quoted is treated as a rejection.
- Holds and commits are sent only to registry addresses. Replies from any other sender are ignored.
- One failing seller releases everyone's holds, and the Planner reroutes around that seller.

## 4. Money stays with the platform until the end

Buyers pay the Orchestrator's escrow, never a seller. Sellers are paid their quoted subtotal only
after every booking in the bundle commits. A seller that misbehaves at the last step gets nothing,
and the buyer can retry or take a refund.

Campus Rooms is never paid and never takes part in escrow. Its lines are free, and the `free:`
commit reference is accepted only when every hold in the round is a $0 Campus Rooms hold.

## 5. Reading the public web safely

The Campus Researcher, Verification's housing link and events feed re-checks, and Campus Rooms
are the only parts that fetch web pages. The rules are in [resourcepilot/research.py](../resourcepilot/research.py)
and [resourcepilot/http.py](../resourcepilot/http.py):

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

## Limits of this demo

- The five demo sellers are demo businesses. Their "verified" flags are demo onboarding markers,
  not the result of a real business-identity check.
- Campus Rooms is "verified" because it runs from this repository. The rooms are real, but
  ResourcePilot cannot see a school's booking rules (who may book, limits per day), so the student
  books on the school's site.
- Wellness rooms and events come only from LibCal and Localist. A school that lists wellness
  spaces elsewhere, or runs another calendar, shows none, and the keyword filter can include
  events a school tags as wellness but a student would not.
- The crawl recognizes common systems and booking words. A school whose library hides its booking
  system behind a sign-in or JavaScript-only navigation gets the paste-link card.
- A production registry would add domain or business verification, signed listings, dispute
  handling, and payout holds for new sellers.
- Almanac discovery is off by default and has been tested only against a simulated Almanac.
