"""
Capture one moneyline, one spread, one total in markets_nfl_sample.json.
Also hunt for futures markets (Super Bowl, division winner, win totals).
"""
import json, requests
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent
BASE = "https://gamma-api.polymarket.com"
NFL_TAG = 450

def get(path, **params):
    r = requests.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

combined = {}
for mtype in ("moneyline", "spreads", "totals"):
    r = get("/markets/keyset", tag_id=NFL_TAG, sports_market_types=mtype, limit=3, closed=False)
    items = r.get("markets", r) if isinstance(r, dict) else r
    if items:
        combined[mtype] = items[0]
        print(f"{mtype}: conditionId={items[0].get('conditionId')} q={items[0].get('question','')[:80]}")

(OUT / "markets_nfl_sample.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
print("\nSaved markets_nfl_sample.json (one of each type)")

# --- Inspect outcomePrices, clobTokenIds ---
for mtype, m in combined.items():
    print(f"\n-- {mtype} --")
    print(f"  question:       {m.get('question')}")
    print(f"  conditionId:    {m.get('conditionId')}")
    print(f"  clobTokenIds:   {m.get('clobTokenIds')}")
    print(f"  outcomes:       {m.get('outcomes')}")
    print(f"  outcomePrices:  {m.get('outcomePrices')}")
    print(f"  volume:         {m.get('volume')}")
    print(f"  endDate:        {m.get('endDate')}")
    print(f"  closedTime:     {m.get('closedTime')}")
    print(f"  groupItemTitle: {m.get('groupItemTitle')}")
    print(f"  spread:         {m.get('spread')}")

# --- Futures: search for Super Bowl / win totals ---
print("\n\n--- Searching for futures markets ---")
# Try searching by tag 450 but look for closed=True historical futures or open futures
futures_candidates = []

# Search events for known futures keywords
for kw in ("Super Bowl", "division", "win total", "AFC", "NFC"):
    try:
        r = requests.get(f"{BASE}/public-search", params={"q": f"NFL {kw}", "limit": 5}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            events = data.get("events", [])
            print(f"  '{kw}': {len(events)} event results")
            for e in events[:2]:
                print(f"    {e.get('title','')[:80]}  tags={[t.get('id') for t in e.get('tags',[])]}")
    except Exception as ex:
        print(f"  '{kw}': {ex}")

# Also check /events/keyset with tag 450 limit 100 for futures-like titles
r2 = requests.get(f"{BASE}/events/keyset", params={"tag_id": NFL_TAG, "limit": 100, "closed": False}, timeout=15)
all_events = r2.json() if r2.status_code == 200 else {}
ev_list = all_events.get("events", all_events) if isinstance(all_events, dict) else all_events
futures = [e for e in ev_list if any(kw in e.get("title","").lower() for kw in ["super bowl","division","win total","afc","nfc","mvp","champion"])]
print(f"\nFutures-like events under tag 450 (open): {len(futures)}")
for e in futures[:5]:
    print(f"  {e.get('title','')[:80]}  markets={len(e.get('markets',[]))}")

if futures:
    (OUT / "futures_market_sample.json").write_text(json.dumps(futures[:3], indent=2), encoding="utf-8")
    print("Saved futures_market_sample.json")
else:
    # Try with closed=True to find recent completed futures
    r3 = requests.get(f"{BASE}/events/keyset", params={"tag_id": NFL_TAG, "limit": 50, "closed": True}, timeout=15)
    closed_ev = r3.json() if r3.status_code == 200 else {}
    closed_list = closed_ev.get("events", closed_ev) if isinstance(closed_ev, dict) else closed_ev
    closed_futures = [e for e in closed_list if any(kw in e.get("title","").lower() for kw in ["super bowl","division","win total","afc","nfc","mvp","champion"])]
    print(f"Futures-like events under tag 450 (closed): {len(closed_futures)}")
    for e in closed_futures[:5]:
        print(f"  {e.get('title','')[:80]}")
    if closed_futures:
        (OUT / "futures_market_sample.json").write_text(json.dumps(closed_futures[:3], indent=2), encoding="utf-8")
        print("Saved futures_market_sample.json (from closed events)")
    else:
        print("No futures found under NFL tag 450 — saving note")
        (OUT / "futures_market_sample.json").write_text(json.dumps({"note": "No futures markets found under NFL primaryTagId=450 in open or recent closed events. NFL season may not have started yet."}, indent=2), encoding="utf-8")
