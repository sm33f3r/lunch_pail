"""Fetch NFL events and markets by tag_id=450, save fixtures."""
import json, requests
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent
BASE = "https://gamma-api.polymarket.com"
NFL_TAG = 450

def get(path, **params):
    r = requests.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    print(f"GET {path} {params} -> {r.status_code}")
    return r.json()

# --- Events (keyset pagination) ---
events = get("/events/keyset", tag_id=NFL_TAG, limit=20, closed=False)
(OUT / "events_nfl_sample.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
print(f"Saved events_nfl_sample.json  ({len(events.get('events', events) if isinstance(events, dict) else events)} items)\n")

# --- All markets (moneyline + spread + totals) ---
all_markets = get("/markets/keyset", tag_id=NFL_TAG, limit=20, closed=False)
(OUT / "markets_nfl_sample.json").write_text(json.dumps(all_markets, indent=2), encoding="utf-8")
print(f"Saved markets_nfl_sample.json\n")

# --- Try sports_market_types filters ---
for mtype in ("moneyline", "spreads", "totals"):
    try:
        r = get("/markets/keyset", tag_id=NFL_TAG, sports_market_types=mtype, limit=5, closed=False)
        items = r.get("markets", r) if isinstance(r, dict) else r
        print(f"  {mtype}: {len(items)} markets returned")
    except Exception as e:
        print(f"  {mtype}: ERROR {e}")

# --- Dump a quick preview of first event ---
ev_list = events.get("events", events) if isinstance(events, dict) else events
if ev_list:
    print("\nFirst event keys:", list(ev_list[0].keys()))
    first_mkts = ev_list[0].get("markets", [])
    if first_mkts:
        print("First market keys:", list(first_mkts[0].keys()))
