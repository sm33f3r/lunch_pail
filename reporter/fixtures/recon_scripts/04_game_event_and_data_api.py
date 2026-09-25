"""
Save the Eagles-Bears game event as the canonical game fixture.
Then hit the Data API for open interest and trades.
"""
import json, requests
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent
GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"

# ── 1. Save canonical game event ──────────────────────────────────────────────
r = requests.get(f"{GAMMA}/events/slug/nfl-phi-chi-2026-09-29", timeout=15)
game_event = r.json()
(OUT / "events_nfl_sample.json").write_text(json.dumps(game_event, indent=2), encoding="utf-8")
print(f"Saved events_nfl_sample.json  title={game_event.get('title')}  gameId={game_event.get('gameId')}")

# Gather condition IDs from the game markets
mkts = game_event.get("markets", [])
condition_ids = [m["conditionId"] for m in mkts if m.get("conditionId")]
print(f"  {len(mkts)} markets, {len(condition_ids)} with conditionId")

# ── 2. Open Interest ──────────────────────────────────────────────────────────
# Docs say /oi with ?condition= param; test with 1, 5, 20 condition IDs
def try_oi(ids):
    joined = ",".join(ids)
    r = requests.get(f"{DATA}/oi", params={"condition": joined}, timeout=15)
    return r.status_code, r.headers, r.json() if r.headers.get("content-type","").startswith("application/json") else r.text

# Single
status1, hdrs1, body1 = try_oi(condition_ids[:1])
print(f"\nOI single ({condition_ids[0][:16]}…): status={status1}")
print("  body:", json.dumps(body1)[:400])
print("  rate-limit headers:", {k:v for k,v in hdrs1.items() if "limit" in k.lower() or "rate" in k.lower()})

# Batch of 5
status5, hdrs5, body5 = try_oi(condition_ids[:5])
print(f"\nOI batch-5: status={status5}")
print("  body:", json.dumps(body5)[:400])

# Batch of 20
status20, hdrs20, body20 = try_oi(condition_ids[:20])
print(f"\nOI batch-20: status={status20}")
print("  body type:", type(body20).__name__, "len:", len(body20) if isinstance(body20, (list, dict)) else "n/a")

# Save the batch-20 (or whatever worked best)
best_oi = body20 if status20 == 200 else (body5 if status5 == 200 else body1)
(OUT / "open_interest_sample.json").write_text(json.dumps(best_oi, indent=2), encoding="utf-8")
print(f"\nSaved open_interest_sample.json")

# ── 3. Trades ─────────────────────────────────────────────────────────────────
cid = condition_ids[0]
# Try different param names: condition, market, conditionId
for param in ("condition", "market", "conditionId"):
    tr = requests.get(f"{DATA}/trades", params={param: cid, "limit": 5}, timeout=15)
    print(f"\nTrades ?{param}={cid[:16]}…  status={tr.status_code}")
    if tr.status_code == 200:
        body = tr.json()
        print("  body:", json.dumps(body)[:400])
        print("  headers:", {k:v for k,v in tr.headers.items() if "limit" in k.lower() or "cursor" in k.lower() or "rate" in k.lower()})
        (OUT / "trades_sample.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        print("  Saved trades_sample.json")
        break
    else:
        print("  response:", tr.text[:200])
