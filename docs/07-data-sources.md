# 07 — Data Sources

A connector catalogue, ordered by **investigative value per unit of cost and
integration effort**. The organizing principle: Astral Lens must be genuinely
powerful for an operator with **no paid API budget at all**, because that is
the common case for the self-hoster this project targets.

All prices, quotas and terms were checked in November 2026 and **will drift**.
Where sources conflicted, both figures are recorded. Each connector's manifest
carries its own quota and licence metadata so the platform surfaces staleness
rather than hiding it.

## 1. Tier 1 — build these first

Free or near-free, keyless or simple-key, stable schemas, high unique value.
This set alone makes a capable platform.

| Source | Access | Limits / licence |
|---|---|---|
| **GLEIF LEI** | Keyless | 60 req/min; data **CC0**. Entity identity backbone |
| **OFAC SDN + Consolidated** | Free download | Public domain. Crypto addresses tagged `Digital Currency Address - CHAIN` |
| **ICIJ Offshore Leaks** | Free bulk CSV + Neo4j dump | ODbL / CC BY-SA, attribution required. ~810k entities, five leaks |
| **UK Companies House** | Free key | 600 req / 5 min; monthly bulk snapshot + streaming API |
| **SEC EDGAR** | Keyless | 10 req/s; **User-Agent with contact required** or 403/429 |
| **TED (EU procurement)** | Keyless to read | Commercial reuse permitted with attribution (Decision 2011/833/EU) |
| **GDELT DOC 2.0** | Keyless | 1 req / 5 s; rolling ~3-month window; use bulk for history |
| **Common Crawl** | Free on S3 | CDX index heavily rate-limited; WARCs via S3 |
| **Wayback CDX** | Keyless | **No published rate limit** (checked 2026-11-05: the CDX README states none, only a server-side ceiling of 150,000 results; an Internet Archive post of Sept 2026 confirms 429 protections without figures). Honour 429 and `Retry-After`, send a descriptive User-Agent. The earlier "30–60 rpm" and "1-year window, 1,000 entries" figures were unsupported and are withdrawn |
| **Copernicus / Sentinel Hub** | Free registration | **Free for all purposes incl. commercial**, with attribution |
| **NASA FIRMS** | Free MAP_KEY | 5,000 transactions / rolling 10 min |
| **OSM Overpass** + **Overture** | Keyless | ODbL; fair use ~10k req/day, ~1 GB/day. Overture Places is CDLA-Permissive-2.0; other themes ODbL |
| **adsb.lol** | Keyless | **ODbL**, no current rate limit, historical dumps available |
| **AISStream** | Free key | WebSocket; subscription message within 3 s; real-time only, no history |
| **Mapillary / KartaView** | Free token / keyless | Imagery CC BY-SA 4.0 — street-level ground truth |
| **HIBP Pwned Passwords** | Keyless | **k-anonymity** (5-char SHA-1 prefix only). The responsible credential check |
| **Blockstream Esplora / mempool.space** | Keyless | Rate-limited, 429s common. Both self-hostable (MIT / AGPL-3.0) |
| **Etherscan API V2** | Free key | 3 req/s, 100k/day across supported chains |
| **Censys** | Free tier | 100 credits/month, non-rollover; 5 free actions/day |
| **SearXNG** (self-hosted) | Free | AGPL-3.0. The meta-search backend — see §6 |

**OpenSanctions** deserves separate mention: ~1.8M entities from 482 sources,
**natively in the FollowTheMoney schema we already use** (doc 03), so it maps in
with no translation layer. Bulk download needs no signup. But the dataset is
**CC BY-NC 4.0** — commercial use, *including compliance screening*, requires a
paid licence. Free API keys are granted for journalism, anti-corruption and
academic work. So: ship the connector, surface the licence prominently, let the
operator decide.

## 2. Tier 2 — high value, costs money or effort

- **Infrastructure:** Shodan (Membership is **$49 one-time, lifetime**, with
  100 query and 100 scan credits/month — read from the billing page on
  2026-11-05; the "$69" figure is the monthly Freelancer plan; official
  `shodan` client). ZoomEye (10k results/month free). BinaryEdge (250
  queries/month free, from $10/mo). Netlas (50 searches/day free). FOFA and
  SecurityTrails are effectively paid-only.
- **Certificate Transparency / BGP / passive DNS:** researched on 2026-11-05;
  see §9 for access methods, limits and, importantly, **terms** — several
  "free" sources here are non-commercial only.
- **Courts:** CourtListener / RECAP is free (since May 2026 free accounts
  report 5/min, 50/hr, 125/day; higher with membership). RECAP Fetch incurs
  PACER fees on a separate quota.
- **Crypto attribution:** **GraphSense** (MIT, BTC/ETH/Tron, TagPacks in git)
  is the only open address-clustering option. Chainalysis, TRM and Elliptic are
  enterprise-gated. Treat all attribution as incomplete — false positives and
  negatives in sanctions screening are expected, and the UI must grade them
  accordingly.
- **Search APIs:** **Brave Search** at $5 per 1,000 requests with ~$5 monthly
  free credit, 50 QPS; storing results or training requires a plan with storage
  rights.

## 3. The search-engine problem

The programmatic-search landscape collapsed in 2025–26 and this changes
architecture, not just configuration:

- **Bing Search API retired 11 Aug 2025.**
- **Google Programmable Search JSON API** closed to new customers 20 Jan 2026,
  with discontinuation reported for 1 Jan 2027 (Google's own announcement was
  not retrieved; verify).
- Remaining routes are paid aggregators (Brave, Serper, SerpApi, Exa, Tavily)
  or self-hosted SearXNG.
- SERP-scraping vendors carry live legal exposure: Reddit's DMCA
  anti-circumvention claims against SerpApi reportedly survived dismissal in
  July 2026.

**Design consequence:** search is an *interface* with several backends
(SearXNG, Brave, any aggregator, or none), and no pipeline may assume
general web search exists. SearXNG self-hosted is the default, with the honest
caveat that Google blocks many instances and upstream engine terms still apply.

## 4. Social platforms — realistic assessment

The gap between what practitioners want and what is lawfully obtainable is
widest here.

| Platform | Reality in 2026 |
|---|---|
| **Bluesky** | **The one genuinely open firehose.** Jetstream is a public WebSocket, no auth. Write limits apply to posting, not reading |
| **Mastodon / ActivityPub** | Default 300 requests / 5 min per account and per IP, with `X-RateLimit-*` headers; `limit` max 40. Public and hashtag timelines are anonymous unless the instance disables public preview; full-text status search needs a user token. Per-instance terms apply (primary docs, 2026-11-05) |
| **YouTube Data API** | 10,000 units/day; `search.list` costs 100 units per call and paged requests cost again, so ~100 searches/day. Extension by form |
| **Telegram** | API free with your own `api_id`. ToS forbid acting without consent and **prohibit AI/ML training on data obtained through Telegram**. Userbots conflict with a strict reading. Permanent bans for flooding |
| **X / Twitter** | Pay-per-use since Feb 2026, no free tier ($20 starter credit for new payers). Post read $0.005, user read $0.010. Capped at **3M** post reads per billing cycle (primary pricing page, 2026-11-05; the "2M" figure appears nowhere on it). No free or Basic tier is listed |
| **Reddit** | Access requires approval under the Responsible Builder Policy; ~100 req/min non-commercial. **Commercial use requires a separate written agreement.** Currently litigating against scrapers |
| **Discord** | Developer Policy: *"Do not mine or scrape any data, content, or information available on or through Discord services."* Self-bots prohibited |
| **Meta** | CrowdTangle shut 14 Aug 2024. Content Library is **academic/nonprofit only** via ICPSR, capped at 500,000 records per 7 days |
| **TikTok** | Research API restricted to vetted academic/nonprofit researchers in listed regions; **commercial use prohibited**; ~4-week review |

Astral Lens ships connectors for the open ones (Bluesky, Mastodon, Telegram
within its terms, YouTube within quota) and **credential-shaped connectors**
for the gated ones: the operator supplies their own approved access, and the
platform never pretends to grant access it cannot grant.

## 5. Scraping: the legal landscape

Factual summary. US first; EU and UK follow in §5.1 (researched 2026-11-05,
not legal advice).

- **Van Buren v. United States** (2021, 6–3): "exceeds authorized access" under
  the CFAA covers only areas a user may not enter at all — the
  "gates-up-or-down" reading. **Violating purpose-based terms of service is not
  CFAA liability.**
- **hiQ v. LinkedIn** (9th Cir. 2019, reaffirmed 2022): the CFAA likely does
  not reach scraping of publicly accessible data. **But LinkedIn then won on
  breach of contract**, and the Dec 2022 consent judgment carried a permanent
  injunction and $500,000. hiQ closed. The lesson is precise: criminal exposure
  receded, contract exposure did not.
- **Meta v. Bright Data** (N.D. Cal., Jan 2024): summary judgment for Bright
  Data — Meta's terms prohibited only *logged-in* scraping.
- **Reddit v. SerpApi / Oxylabs / Perplexity**: DMCA anti-circumvention theories
  are the new front, reportedly surviving dismissal in July 2026 (single
  secondary report; not checked against dockets).

Synthesis: logged-out collection of public pages has favourable Ninth Circuit
precedent; the live risks are **contract terms and anti-circumvention**. This
is why the platform's firm technical line is that it **never bypasses
authentication or anti-bot controls** — that line tracks where the legal risk
actually concentrates, not squeamishness.

### 5.1 EU and UK

The US analysis turns on contract and anti-circumvention. In Europe the
centre of gravity is **data protection**: scraped public personal data is still
personal data. The self-hosted operator is probably the *controller*
(GDPR Art. 4(7)), and the household exemption (Art. 2(2)(c)) is narrow for
professional or case work (inference). The tool's author is probably not a
controller unless they offer hosted processing (not verified).

**Settled**

- Public personal data is within GDPR scope; Art. 9 applies to scraped
  special-category data. The CJEU reads "manifestly made public" (Art. 9(2)(e))
  narrowly (*Schrems v Meta*, C-446/21, 4 Oct 2024 — secondary summary).
- The ICO and eleven other authorities stated jointly (24 Aug 2023) that
  publicly accessible personal information still needs a lawful basis.
- **EU AI Act Art. 5(1)(e)** prohibits AI systems that create or expand facial
  recognition databases through untargeted scraping of facial images from the
  internet or CCTV. No law-enforcement exception (contrast Art. 5(1)(h)).
  Wording read from an aggregator, not EUR-Lex; whether a tool that only
  matches against existing images is in scope is interpretive and unverified.
- *Ryanair v PR Aviation* (C-30/14, 15 Jan 2015): where the Database Directive
  does not apply, website terms can restrict screen-scraping. Contract exposure
  is therefore real in the EU as in the US.
- UK Computer Misuse Act 1990 s.1 has no statutory defence for scraping, and
  s.17(5) defines unauthorised access by entitlement and consent. Whether
  revoking permission through terms makes scraping "unauthorised": no UK
  authority verified.
- Clearview AI fines for lack of legal basis, transparency failures, no
  retention limit and ignored rights requests: Italy €20m, Greece €20m, France
  €20m (from memory), Netherlands €30.5m (3 Sept 2024).

**Contested**

| Question | Positions |
|---|---|
| Is scraping justified by legitimate interest? | **CNIL** (focus sheet, 5 Jan 2026, read directly): "not prohibited per se", generally legitimate interest, *with* mandatory safeguards — predefined collection criteria, filtering unneeded categories, prompt deletion of the irrelevant, excluding sites that object, publishing a source list. **Dutch AP** (guidance 1 May 2024, press summaries only): private-party scraping is "almost never" permitted. |
| Art. 14(5)(b) "disproportionate effort" | Text checked; not limited to research. For *targeted* OSINT on a named individual the argument is weak (inference). |
| Machine-readable TDM opt-out (DSM Art. 4) | *Kneschke v LAION* (OLG Hamburg, 10 Dec 2025): a natural-language reservation was not shown to be machine-readable; further appeal allowed. |
| UK Clearview | ICO £7.5m fine (2022); tribunal held no jurisdiction (2023); Upper Tribunal [2025] UKUT 319 (AAC) found the FTT erred and remitted. Permission to appeal reportedly granted; no Court of Appeal judgment found. Treat the fine as unresolved. |
| UK legitimate interest | Data (Use and Access) Act 2025 adds "recognised legitimate interests", but scraping is unlikely to fall within them, so a full balancing test is still needed (inference). |
| Journalism exemption | DPA 2018 Sch. 2 para. 26 needs a reasonable belief that publication is in the public interest. Not a default for a non-publishing investigator. |

**Not a route:** DSA Art. 40 researcher access is limited to vetted
researchers studying systemic risks; it is not a data source for OSINT.

**What this means for the design**

- Store a **documented legitimate-interest assessment per collection purpose**
  on the case record, alongside its retention period (a missing retention
  limit was cited in enforcement).
- Record **source domain and URL per item** (already in `acquisition_ctx`), so
  an Art. 14 notice or a published source list can be produced.
- Honour robots.txt, CAPTCHAs and blocks and log the check — consistent with
  the existing no-bypass line.
- Prefer exclusion of Art. 9 content at collection time and prompt purging of
  the irrelevant. **Deleting data soon after collection does not cure a
  missing legal basis.**
- Hashing or pseudonymising is not anonymising: personal-data status depends
  on who holds means of identification (CJEU C-413/23 P *EDPS v SRB*,
  4 Sept 2025 — secondary), and the operator does.
- Keep a **per-source terms and database-right register**; this is what the
  source registry's licence fields are for.
- No untargeted facial-image crawling feeding face matching (Art. 5(1)(e)).

**Could not be verified** (do not rely on): EUR-Lex text of AI Act Art. 5 and
its application date; any Commission guidelines on Art. 5(1)(e); the full text
of the EDPB scraping guidelines (draft 03/2026, scoped to generative AI) and
Guidelines 1/2024 adoption status; the ICO's Clearview statement; the Dutch AP
text; the outcome of the Garante's €15m OpenAI fine on appeal; whether the 2026
Digital Omnibus changes AI Act Art. 5 or the GDPR; enactment of UK Computer
Misuse Act reform; UK case law on terms-based claims against scrapers; newer
CJEU database-right rulings; member-state journalism laws under Art. 85.

Collection hygiene, encoded in the plugin contract:

- Per-host token buckets; honour `Crawl-delay` even though **RFC 9309 does not
  define it**; back off on 429.
- **Per-case egress identity.** A single shared IP across all cases links every
  investigation the operator has ever run, to anyone watching. Egress identity
  belongs to the case.
- Record which persona and egress served each request, in the artifact's
  acquisition context.

## 6. Licence traps in data, not just code

These bite at distribution time, which is after the code is written:

| Source | Trap |
|---|---|
| **VirusTotal public API** | *"must not be used in commercial products or services"*; business workflows that don't contribute new files prohibited; multi-account evasion prohibited, with permanent bans |
| **MaxMind GeoLite2** | Account + licence key required; **redistribution restricted** — open-source projects are actively removing bundled copies. Do not vendor the database |
| **OpenSanctions** | CC BY-NC — commercial use including compliance screening needs a licence |
| **adsb.fi / OpenSky** | Non-commercial only; adsb.fi 1 req/s and requires citation |
| **AlienVault OTX** | EULA reportedly limits use to non-commercial purposes (unverified) |
| **Google Earth Engine** | Free for non-commercial/research; commercial use requires a paid Cloud account |
| **MediaCloud** | Academic, nonprofit and journalistic research only; no key sublicensing; 4,000 req/week |
| **Overture / OSM** | ODbL **share-alike applies to derivative databases** — relevant because our entity store is a database |
| **WhatsMyName** | Dataset CC BY-SA 4.0 — derived site packs are share-alike (see doc 04) |
| **IPinfo Lite** | Free and unlimited for country/ASN, but CC BY-SA 4.0 with attribution |

The **source registry** mirrors the model registry: every connector row carries
licence, SPDX where applicable, commercial-use flag, attribution requirement
and quota. Preflight can then answer "is this deployment licence-clean for my
use?" — a question no existing OSINT platform answers.

## 7. Sensitive categories

In scope, reported factually, with the operational reality stated plainly.

### Breach and credential data

- **Low-risk and recommended default:** HIBP **Pwned Passwords** via
  k-anonymity — only a 5-character SHA-1 prefix leaves the box. Genuinely
  privacy-preserving.
- **HIBP breach search** needs a paid key (read from the subscription page,
  annual billing, 2026-11-05): Core 1 is 10 RPM at $4.39/mo and covers **your
  own domains only**; Core 5 is 1,000 RPM at $319/mo; Pro 1 (1,000 RPM,
  $379/mo) adds third-party domain search; High RPM 4000 is $1,150/mo.
  Stealer-log domain APIs are gated to the top tier. The old "~$3.50/mo"
  figure is superseded. The API-key page itself is behind sign-in. Breach and
  paste data is CC BY 4.0 (attribution required). **Pwned Passwords**
  (`/range/{prefix}`) is keyless, unlimited, needs a User-Agent (403 without),
  supports `Add-Padding`, and states no licence or attribution requirement.
- **Full-record services:** DeHashed (~$180–290/mo, ~$0.02/query
  pay-as-you-go), LeakCheck (keyless public API returns *breach names and
  categories only*; full records need a paid Pro key; password and domain
  search are Enterprise), IntelX (~€5,000/yr API), Snusbase, Hudson Rock
  Cavalier (**keyless community API, 50 req/10 s** — the cheapest useful entry
  point).
- **Honest risk statement:** these corpora contain personal data and
  credentials of unknown and often criminal provenance; vendor terms restrict
  redistribution; and some jurisdictions may treat possession or processing of
  stolen data as an offence. No regulator guidance on the GDPR lawful basis for
  breach-corpus aggregation was located — a real gap, not an oversight.
- **Platform behaviour:** connectors are shipped and work without friction.
  What the platform does unconditionally is tag assertions derived from breach
  corpora with their source and a retention timer, because that costs the
  operator nothing and is what makes later deletion possible.

### People-search aggregators

Pipl, People Data Labs, FullContact, Whitepages/Ekata, TLOxp/TruLookup,
IRBsearch, idiCORE, Tracers, Endato, Spokeo.

The structural point that matters architecturally: **GLBA/DPPA-gated data is
released against the subscriber's own certification of permissible purpose,
often per query.** A self-hosted tool cannot make that certification on the
operator's behalf. These are therefore **bring-your-own-credential
pass-through connectors** by necessity, not by policy — the operator holds the
account and makes the certification. Where outputs feed eligibility decisions,
FCRA consumer-reporting obligations attach, and several vendors expressly state
their products are not FCRA consumer reports.

Pipl's permissible-use policy additionally requires a certified investigative
or identity-verification need, bans political-campaign use and grants Pipl
audit rights. Spokeo's consumer terms prohibit scraping and bot queries
outright and it exposes no public API.

### EU beneficial ownership

**CJEU Grand Chamber, 22 Nov 2022 (C-37/20 and C-601/20, *WM and Sovim*)
invalidated general public access to beneficial-ownership registers.** Access
is now limited to legitimate-interest categories, varying by member state.
Republishing that data raises GDPR and Charter exposure. Open Ownership's BODS
standard was not verified and is a gap.

## 8. Connector build order

1. **Identity and corporate backbone** — GLEIF, OpenCorporates (200 calls/month
   free), Companies House, SEC EDGAR, OpenSanctions, OFAC, ICIJ.
2. **Web and archive** — SearXNG, Wayback CDX, GDELT, Common Crawl, plus the
   evidence-grade web capture that doc 04 identifies as an open-source gap.
3. **Infrastructure** — crt.sh, RDAP/WHOIS, Censys free tier, Shodan, passive
   DNS, urlscan.io.
4. **Geospatial** — OSM/Overpass, Overture, Copernicus, FIRMS,
   Mapillary/KartaView, adsb.lol, AISStream.
5. **Site packs** — username and account enumeration as data, per doc 04 §4.
6. **Media** — Bluesky firehose, Mastodon, YouTube, Telegram.
7. **Sensitive** — HIBP Pwned Passwords first; full-record and people-search
   connectors as bring-your-own-credential.
8. **Crypto** — Esplora, mempool.space, Etherscan, Blockchair, GraphSense.

## 9. Gaps closed on 2026-11-05, and gaps that remain

Researched against primary pages where reachable. **P** = primary page,
**S** = search excerpt or third party, **U** = unverified, **G** = gated behind
login. Figures drift; re-check at implementation time.

### Certificate Transparency

| Source | Access | Limits | Terms | Note |
|---|---|---|---|---|
| crt.sh | Web/JSON, no key | 60 req/IP/min nginx throttle (P, a 2020 maintainer post); frequent 502/421/timeouts observed today | No ToS or API doc found (U) | Best-effort lookup; cache aggressively |
| RFC 6962 logs | `get-sth`, `get-entries`, keyless | None in the spec (P) | IETF boilerplate | RFC 6962 is obsolete (replaced by RFC 9162); entries must be verified against the tree head |
| static-ct-api | Static HTTP: checkpoint, tiles, data tiles | Tiles immutable — cache long | Spec licence not checked (U) | **Preferred bulk path**; read log URLs from Google's `log_list/v3` |
| Censys | Platform API v3, Bearer PAT | Free: 100 credits/month, expiring; 5 free actions/day; concurrency 1 | **ToS §3.1 bars Free and Researcher tiers from commercial use** (P) | Non-commercial only on the free tier |

### BGP and passive DNS

| Source | Access | Limits | Terms |
|---|---|---|---|
| RIPEstat | Keyless; send `sourceapp` + contact | No hard limit; 8 concurrent/IP; register above 1,000 req/day (P) | **Commercial use needs RIPE NCC written permission**; attribution; no redistribution (P) |
| bgp.tools | Keyless files + WHOIS | Custom User-Agent required; `table.txt` no more than every 30 min; scraping HTML risks a ban (P) | **Free only for non-commercial personal ASN use**; paid from £25/mo (P) |
| RouteViews | MRT archive | RIB every 2 h, updates every 15 min (P) | CC BY 4.0 (P) |
| RIPE RIS | MRT archive, RIS Live | bview every 8 h, updates every 5 min (P) | Legal text not extracted (U) |
| CAIDA | Account, per-dataset AUA | — | Custom AUA, non-profit research (S). **Do not bundle** |
| CIRCL pDNS | Partner access on request | 1,000-record cap, paginate; no CIDR (P) | Intended-use statement only; gated |
| SecurityTrails | API key | Monthly quota, 429 at exhaustion (P); pricing page 403 (U) | OSINT plan reported non-commercial (S) |
| VirusTotal | Free key | 500/day, 4/min (P) | **Must not be used in commercial products** (P) |
| Farsight DNSDB | — | Community Edition discontinued (S) | Quote-based paid |
| BGPView | Did not resolve (U) | — | Treat as unavailable until re-checked |

**No passive-DNS option is both free and commercially permissive.**

### Sanctions lists

| List | Formats | Update / terms |
|---|---|---|
| UN Consolidated | XML, HTML, PDF; the XML URL redirects to a **time-limited signed blob** (P) | Updated 29 Oct 2026 (S). Site terms allow personal non-commercial download, no resale or derivatives (P); list-specific terms not found (U). Resolve the redirect at run time; never cache the signed URL |
| EU Financial Sanctions (FSF) | XML v1.1 (XSD-based), CSV v1.1, PDF, via RSS (P) | "Whenever necessary" from the Official Journal. **No licence statement** on the Commission page (U) |
| UK Sanctions List | XML (XSD 4.33.3), CSV, ODS, ODT, HTML, TXT, PDF; no JSON (P) | **OGL v3.0** (P). The OFSI list closed 28 Jan 2026; this is the UK source now |
| OFAC SDN | SDN.XML, SDN_ADVANCED.XML, SDN_ENHANCED.XML, CSV, fixed-field (P) | XML namespace changed 7 May 2024 — pin it (P). Public-domain status unverified (U) |

### Ownership and public spending

- **Open Ownership BODS**: v0.4 documented, JSON Schema 2020-12, changes
  expected before 1.0 (P). Release date, schema URL and licence not stated (U);
  pin 0.4 and watch the changelog.
- **USAspending**: API v2 (S). An unauthenticated GET returned 200 (observed);
  no key or rate limit stated (P). Pace requests; terms not stated (U).

### Social, archives, TV

- **Mastodon**: see §4 (primary docs).
- **archive.today**: could not be fetched and no policy found (U). **Do not
  automate until its policy has been read.**
- **Internet Archive TV News**: no primary API documentation found (U).
  Keep behind a feature flag.

### Conflicting figures, resolved

| Figure | Resolution |
|---|---|
| Shodan membership | **$49 one-time, lifetime** (P); $69 is the monthly Freelancer plan |
| GreyNoise | Unauthenticated: 10 IP lookups/day; community accounts: 50 searches/week shared with the Visualizer (P). No free-account daily figure exists |
| Wayback | No published rate limit; see §1 |
| X API | 3M post reads per cycle; see §4 |
| Blockchair | Keyless 1,000 calls/day with IP block on excess (S — docs returned 401) |
| HIBP | See §7 |

### Remaining gaps

Not researched in this pass: land registries; VK and 4chan; podcast indexes;
Landsat/Planet/Maxar; rail and transit. Gated or unreachable: HIBP API key
page, Shodan checkout, Censys enterprise pricing, archive.ph, bgpview.io,
Shodan developer pricing, SecurityTrails pricing, Blockchair docs and plans,
farsightsecurity.com. Unverified licence text: OFAC, UN, EU lists; RIS; CAIDA
AUA; BODS.
