"""
Recon: what does GET /oi?market=<conditionId> return for zero-OI and
not-found markets?

Decides whether an empty list [] from /oi can be read as "confirmed zero OI"
or must stay an error (see fetch_open_interest() in watcher/market_data.py).

Touches current/live market state only. Prints raw status + body for each
probe; nothing is written to disk.

Run from a host Polymarket does not geo-block (e.g. the Coolify container):
    python reporter/fixtures/recon_scripts/06_oi_empty_response.py
"""
import json, requests

GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"


def probe(label, condition_id):
    r = requests.get(f"{DATA}/oi", params={"market": condition_id}, timeout=15)
    try:
        body = json.dumps(r.json())
    except ValueError:
        body = repr(r.text[:300])
    print(f"[{label}] market={condition_id!r}\n    status={r.status_code}  body={body[:300]}")


# ── 1. Zero-OI candidates: newest open markets with no recorded volume ───────
r = requests.get(
    f"{GAMMA}/markets",
    params={"order": "createdAt", "ascending": "false", "limit": 100, "closed": "false"},
    timeout=15,
)
r.raise_for_status()
zero_vol = [m for m in r.json() if m.get("conditionId") and float(m.get("volumeNum") or 0) == 0]
print(f"{len(zero_vol)} newest open markets with volumeNum == 0\n")
for m in zero_vol[:5]:
    print(f"  question={m.get('question', '')[:60]!r}  createdAt={m.get('createdAt')}")
    probe("zero-volume", m["conditionId"])

# ── 2. Control: a known-active market (highest 24h volume) ───────────────────
r = requests.get(
    f"{GAMMA}/markets",
    params={"order": "volume24hr", "ascending": "false", "limit": 1, "closed": "false"},
    timeout=15,
)
r.raise_for_status()
active = r.json()[0]
print(f"\n  question={active.get('question', '')[:60]!r}")
probe("active", active["conditionId"])

# ── 3. Not-found / malformed ids ─────────────────────────────────────────────
print()
probe("well-formed, nonexistent", "0x" + "ab" * 32)
probe("malformed short hex",      "0xdeadbeef")
probe("non-hex garbage",          "not-a-condition-id")
probe("empty string",             "")
