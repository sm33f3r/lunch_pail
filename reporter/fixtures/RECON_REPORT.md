# Polymarket API Reconnaissance Report

**Date:** 2026-09-22  
**Status:** Complete — all 8 questions answered, all fixtures captured from live data.  
**NFL games available:** Week 3 slate active (e.g., Eagles vs. Bears, 2026-09-29). Season in progress.

---

## (a) NFL Tag ID and Series Identifier

**Confirmed from `sports_response.json`:**

| Field | Value |
|---|---|
| `sport` | `"nfl"` |
| `id` (sports row id) | `10` |
| `primaryTagId` | `450` ← use this to filter markets/events |
| `tags` | `"1,450,100639"` |
| `series` | `"12185"` (series slug: `nfl-2026`, title: `NFL 2026`) |
| `ordering` | `"away"` — first team in title/slug is the **away** team |
| `resolution` | `https://www.nfl.com/` |

Filter endpoint: `/events/keyset?tag_id=450` and `/markets/keyset?tag_id=450`.

---

## (b) Do Futures Share the NFL Tag? What Distinguishes Them?

**YES — futures markets share `tag_id=450` with individual game markets.**

Confirmed from `/events/keyset?tag_id=450&limit=100`:

- Open futures found under tag 450: "Pro Football: 2027 Champion" (33 markets), "Pro Football: 2027 NFC Champion" (17 markets), "Pro Football: AFC East Champion" (5 markets), "NFL Win Totals: Over or Under?" etc.

**Distinguishing field: `gameId` on the event object.**

| Market type | `gameId` value |
|---|---|
| Individual game | Integer, e.g. `19501` (Eagles vs. Bears) |
| Futures / props | `null` |

Filter logic: `event.gameId is not None` → game market; `event.gameId is None` → futures/prop.

Fixture evidence: `events_nfl_sample.json` (game event, gameId=19501); `futures_market_sample.json` (futures events, all gameId=null).

---

## (c) Confirmed Market Object Field Names and Types

All confirmed present in `markets_nfl_sample.json` and `events_nfl_sample.json`.

### Core identity fields

| Field | Type | Notes |
|---|---|---|
| `id` | string (numeric) | e.g. `"3701260"` |
| `conditionId` | string (hex) | e.g. `"0x3ce1a8eb83552ffab3..."` — used for CLOB and Data API |
| `questionID` | string (hex) | UMA-style question ID |
| `slug` | string | e.g. `"nfl-phi-chi-2026-09-29"` (ML), `"nfl-phi-chi-2026-09-29-spread-home-1pt5"` (spread) |
| `question` | string | Human-readable market title (see Team Identity section) |

### CLOB token IDs

`clobTokenIds`: **JSON-encoded string** containing an array of uint256 token ID strings.  
Example: `'["23346299013290870387...", "85456..."]'`  
→ Parse with `json.loads(market["clobTokenIds"])`.  
Index 0 = first outcome token, index 1 = second outcome token.

### Outcome prices

`outcomePrices`: **JSON-encoded string** containing an array of price strings.  
Example: `'["0.38", "0.62"]'`  
→ Parse with `json.loads(market["outcomePrices"])`.  
Prices sum to ~1.0. Corresponds positionally to `outcomes`.

`outcomes`: **JSON-encoded string** array of outcome labels.  
Example (moneyline): `'["Eagles", "Bears"]'`  
Example (totals): `'["Over", "Under"]'`

### Time fields

| Field | Type | Notes |
|---|---|---|
| `endDate` | ISO-8601 string | e.g. `"2026-09-29T00:15:00Z"` — scheduled close time |
| `endDateIso` | ISO-8601 string | Same value, redundant alias |
| `startDate` / `startDateIso` | ISO-8601 string | Market open time |
| `closedTime` | ISO-8601 string or `null` | Set when market actually closes; null while open |
| `createdAt` / `updatedAt` | ISO-8601 string | Metadata timestamps |
| `acceptingOrdersTimestamp` | ISO-8601 string | When order book went live |

### Home/away ordering

`ordering` is on the **sport object** (from `/sports`), not on the market.  
NFL `ordering = "away"` → the **first team** in the event title and slug is the **away team**.

Examples from live data:
- Event title `"Eagles vs. Bears"` → Eagles = away, Bears = home
- Slug `"nfl-phi-chi-2026-09-29"` → `phi` (Eagles) = away, `chi` (Bears) = home

There is no `homeTeam`/`awayTeam` field on the market or event object — see Team Identity section.

### Market type (moneyline vs. spread vs. total)

No explicit `marketType` enum field on the market object. Determined via:

1. **`sports_market_types` query param** when fetching: `moneyline | spreads | totals` — most reliable for fetching by type.
2. **`groupItemTitle` field**: 
   - Moneyline: `null`
   - Spread: `"Spread -1.5"`, `"Spread -2.5"`, etc.
   - Total: `"O/U 44.5"`, `"O/U 43.5"`, etc.
3. **`question` text parsing** as fallback:
   - Moneyline: `"Eagles vs. Bears"` (two teams, no qualifier)
   - Spread: `"Spread: Bears (-1.5)"`
   - Total: `"Eagles vs. Bears: O/U 44.5"`

### URL construction

- Event URL: `https://polymarket.com/event/{event.slug}`  
  Example: `https://polymarket.com/event/nfl-phi-chi-2026-09-29`
- Market URL: `https://polymarket.com/event/{event.slug}/{market.slug}` (when slugs differ)

---

## (d) Volume Time Windows Available

Confirmed on the **event object** (not individual market):

| Field | Window |
|---|---|
| `volume` | Lifetime (all-time) |
| `volume24hr` | Last 24 hours |
| `volume1wk` | Last 7 days |
| `volume1mo` | Last 30 days |
| `volume1yr` | Last 365 days |
| `openInterest` | Current open interest (shares) |
| `liquidityClob` | Current CLOB liquidity (USDC) |

Step 7's configurable window can be set to any of: `24hr`, `1wk`, `1mo`, `1yr`, or `lifetime` (`volume`).

Individual market objects expose `volume` (lifetime) and `volumeClob` (CLOB lifetime volume). Per-market 24hr/weekly breakdown is NOT on the market object; only on the event.

Fixture evidence: `events_nfl_sample.json` top-level fields.

---

## (e) Open Interest Endpoint Shape

**Confirmed from `open_interest_sample.json`:**

### GET /oi  — **global only, does not filter**

```
GET https://data-api.polymarket.com/oi
Response: [{"market": "GLOBAL", "value": 338436098.59}]
```

- Tested params: `condition=`, `conditionId=`, `market=`, none — all return the same single-item global response.
- This endpoint does **not** support per-condition OI queries. It is a platform-wide counter only.
- Batch limit: N/A — the param is ignored.

### GET /live-volume — per-market OI within an event

```
GET https://data-api.polymarket.com/live-volume?id=<event_id>
Response: [{"total": <float>, "markets": [{"market": "<conditionId>", "value": <float>}, ...]}]
```

- `id` param = Gamma API event integer ID (e.g. `870381` for Eagles vs. Bears).
- Returns all markets in the event in one call; no batching needed.
- `total` = sum of all market values for the event.
- `value` is in shares (not USDC).

Fixture evidence: `open_interest_sample.json` → `per_market_oi_response`.

---

## (e) Open Interest Endpoint Shape Updated Findings

**Corrected 2026-09-23 after manual re-verification — original recon claim was wrong.**

### GET /oi — per-market, with global fallback on invalid id

GET https://data-api.polymarket.com/oi?market=<conditionId>


- Valid conditionId → returns that market's actual OI:
  `[{"market": "0x3ce1a8eb...", "value": 6735.24449}]`
- Invalid/unrecognized conditionId (e.g. `0xdeadbeef`) → falls back to platform-wide aggregate:
  `[{"market": "GLOBAL", "value": 336987405.13}]`

**Correct usage: call `/oi?market=<conditionId>` per market. It does filter — the original recon test used a malformed id and mistook the fallback for default behavior.**

Batch limit: unconfirmed — one conditionId per call was tested; batching multiple ids in one request not yet tried.

### GET /live-volume — event-level, all markets in one call

GET https://data-api.polymarket.com/live-volume?id=<event_id>
Response: [{"total": <float>, "markets": [{"market": "<conditionId>", "value": <float>}, ...]}]


Still valid as an alternative — useful when pulling OI for every market in an event in a single call rather than one `/oi` call per market. Step 6 can choose either: `/oi` per-market-of-interest, or `/live-volume` once per event to get all markets at once.

Fixture evidence: `open_interest_sample.json` (original, now partially superseded — see manual test values above); re-verification done via live curl, not re-saved as fixture.

## (f) Trades Endpoint Shape

**Confirmed from `trades_sample.json`:**

### Request

```
GET https://data-api.polymarket.com/trades?market=<conditionId>&limit=<N>
```

**Correct filter param is `market=` (not `condition=` or `conditionId=` — those are ignored).**

Available filters tested:

| Param | Behavior |
|---|---|
| `market=<conditionId>` | Filters to that market. **Use this.** |
| `condition=<conditionId>` | Ignored — returns global recent trades |
| `conditionId=<conditionId>` | Ignored — returns global recent trades |
| `limit=N` | Page size |
| `offset=N` | Skip N records (standard offset pagination) |
| `before=<timestamp>` | Return trades with timestamp < value |
| `cursor=<timestamp>` | Alias for `before=` |

No pagination metadata in response body or headers — pagination is entirely via `offset` or `before`/`cursor`.

### Response fields (per trade)

| Field | Type | Notes |
|---|---|---|
| `proxyWallet` | string (hex address) | Trader's proxy wallet |
| `side` | string | `"BUY"` or `"SELL"` |
| `asset` | string (uint256) | clobTokenId of the outcome traded |
| `conditionId` | string (hex) | Market condition ID |
| `size` | float | Shares traded |
| `price` | float | Price per share (0–1) |
| `timestamp` | integer | Unix timestamp (seconds) |
| `title` | string | Event title, e.g. `"Eagles vs. Bears"` |
| `slug` | string | Market slug |
| `eventSlug` | string | Parent event slug |
| `outcome` | string | Outcome label, e.g. `"Bears"` |
| `outcomeIndex` | integer | 0 or 1 |
| `name` | string | Trader display name |
| `pseudonym` | string | e.g. `"Huge-Vein"` |
| `bio` | string | |
| `profileImage` | string | URL or empty |
| `profileImageOptimized` | string | URL or empty |
| `transactionHash` | string (hex) | On-chain tx hash |

---

## (g) Rate Limits

**Observed limit: none detected.**

- No rate-limit headers (`X-RateLimit-*`, `Retry-After`, etc.) returned by any endpoint across 20+ calls to Gamma API and Data API.
- No HTTP 429 responses observed during this recon session.
- Official documentation does not document any rate limits for unauthenticated public endpoints.
- **Documented limit: not found in docs (not empirically tested to failure).**

Recommendation: treat as soft-unlimited for low-frequency polling; add exponential backoff on 429 as a safety measure.

---

## (h) Team Identity: Structured or Text Parsing?

**Text parsing required — no structured team fields on market or event objects.**

Neither market objects nor event objects expose `homeTeam`, `awayTeam`, `teams`, or any structured team identifier. The `/teams` endpoint exists on Gamma API but is separate from event/market objects and would require a join.

### Parsing patterns confirmed from live data

NFL `ordering = "away"` → **first-named team is always AWAY**.

| Source field | Example | Away | Home |
|---|---|---|---|
| Event `title` | `"Eagles vs. Bears"` | Eagles | Bears |
| Event `slug` | `"nfl-phi-chi-2026-09-29"` | phi (Eagles) | chi (Bears) |
| Market `question` (ML) | `"Eagles vs. Bears"` | Eagles | Bears |
| Market `question` (total) | `"Eagles vs. Bears: O/U 44.5"` | Eagles | Bears |
| Market `question` (spread) | `"Spread: Bears (-1.5)"` | — (favored team only) | — |

More slug examples from week 3 slate:
- `nfl-lac-buf-2026-09-27` → away=LAC (Chargers), home=BUF (Bills)
- `nfl-ari-sf-2026-09-27` → away=ARI (Cardinals), home=SF (49ers)
- `nfl-sea-was-2026-09-27` → away=SEA (Seahawks), home=WAS (Commanders)
- `nfl-bal-dal-2026-09-27` → away=BAL (Ravens), home=DAL (Cowboys)
- `nfl-kc-mia-2026-09-27` → away=KC (Chiefs), home=MIA (Dolphins)

Slug pattern: `nfl-{away_abbr}-{home_abbr}-{YYYY-MM-DD}` (for the main game event).

---

## Open Questions Resolved

| Question | Answer |
|---|---|
| **(a) Volume window options** | 24hr, 1wk, 1mo, 1yr, lifetime — all available on event object |
| **(b) Futures share NFL tag?** | Yes. Distinguisher: `event.gameId` (null for futures, integer for games) |
| **(c) Team identity structured or parsed?** | Text parsing required. Pattern: `{away} vs. {away} vs. {home}` in title, `nfl-{away_abbr}-{home_abbr}-{date}` in slug |

---

## Fixtures Saved

| File | Description |
|---|---|
| `sports_response.json` | Full `/sports` response (469 entries) |
| `events_nfl_sample.json` | Eagles vs. Bears game event (gameId=19501, 53 markets) |
| `markets_nfl_sample.json` | One moneyline, one spread, one total (keyed by type) |
| `open_interest_sample.json` | /oi global + /live-volume per-market breakdown for Eagles vs. Bears event |
| `trades_sample.json` | 10 Eagles vs. Bears moneyline trades (market= param confirmed) |
| `futures_market_sample.json` | 3 NFL futures events (gameId=null): 2027 Champion, 2027 NFC Champion, 2027 AFC Champion |
