"""
Deep-dive on Data API: test OI per-condition, trades filtering,
pagination, and all available params.
"""
import json, requests
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent
DATA = "https://data-api.polymarket.com"

# Load our known NFL market condition IDs
game = json.loads((OUT / "events_nfl_sample.json").read_text(encoding="utf-8"))
mkts = game.get("markets", [])
moneyline_cid = next((m["conditionId"] for m in mkts if m.get("question","") == "Eagles vs. Bears"), None)
spread_cid    = next((m["conditionId"] for m in mkts if "Spread" in m.get("question","") and "-1.5" in m.get("question","")), None)
print(f"moneyline conditionId: {moneyline_cid}")
print(f"spread conditionId:    {spread_cid}")

# ── OI per-condition ──────────────────────────────────────────────────────────
print("\n── Open Interest tests ──")
for label, params in [
    ("global only", {}),
    ("condition (ML)", {"condition": moneyline_cid}),
    ("condition_id",   {"condition_id": moneyline_cid}),
    ("conditionId",    {"conditionId": moneyline_cid}),
    ("market (ML)",    {"market": moneyline_cid}),
]:
    r = requests.get(f"{DATA}/oi", params=params, timeout=15)
    print(f"  {label}: status={r.status_code}  body={r.text[:200]}")

# Also test batch: comma-separated list in condition param
all_cids = [m["conditionId"] for m in mkts if m.get("conditionId")]
for batch in (1, 5, 10, 20, 25):
    subset = all_cids[:batch]
    r = requests.get(f"{DATA}/oi", params={"condition": ",".join(subset)}, timeout=15)
    body = r.json() if r.status_code == 200 else r.text
    print(f"  batch={batch}: status={r.status_code}  result={json.dumps(body)[:150]}")

# ── Trades: multiple param patterns ──────────────────────────────────────────
print("\n── Trades tests ──")
for label, params in [
    ("condition (ML)",   {"condition": moneyline_cid, "limit": 5}),
    ("conditionId (ML)", {"conditionId": moneyline_cid, "limit": 5}),
    ("market (ML)",      {"market": moneyline_cid, "limit": 5}),
    ("asset (ML clobTokenId)", {}),  # placeholder — filled below
]:
    if label.startswith("asset"):
        # get clobTokenIds for ML market
        ml_mkt = next((m for m in mkts if m.get("question","") == "Eagles vs. Bears"), None)
        if ml_mkt:
            ctids = json.loads(ml_mkt.get("clobTokenIds","[]"))
            params = {"asset": ctids[0], "limit": 5} if ctids else {}
        if not params:
            continue
    r = requests.get(f"{DATA}/trades", params=params, timeout=15)
    print(f"  {label}: status={r.status_code}")
    if r.status_code == 200:
        body = r.json()
        print(f"    count={len(body) if isinstance(body, list) else 'dict'}  sample={json.dumps(body[0] if isinstance(body,list) and body else body)[:250]}")

# ── Trades pagination ─────────────────────────────────────────────────────────
print("\n── Trades pagination ──")
r1 = requests.get(f"{DATA}/trades", params={"condition": moneyline_cid, "limit": 3}, timeout=15)
if r1.status_code == 200:
    body1 = r1.json()
    print(f"  page1 ({len(body1) if isinstance(body1,list) else '?'} items): {json.dumps(body1)[:400]}")
    # Check for cursor/next_cursor in response or headers
    print(f"  headers: {dict(r1.headers)}")

# ── Save final verified trades fixture ───────────────────────────────────────
# Use whichever param that returned NFL trades
for p_name in ("condition", "conditionId", "market"):
    r = requests.get(f"{DATA}/trades", params={p_name: moneyline_cid, "limit": 10}, timeout=15)
    if r.status_code == 200:
        body = r.json()
        # Check if any result matches our conditionId
        if isinstance(body, list):
            nfl_trades = [t for t in body if t.get("conditionId") == moneyline_cid or t.get("condition_id") == moneyline_cid]
            all_cids_in_response = list({t.get("conditionId") for t in body})
            print(f"\n  {p_name}: {len(body)} trades, {len(nfl_trades)} matching NFL conditionId")
            print(f"  conditionIds in response: {all_cids_in_response[:3]}")
        (OUT / "trades_sample.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        print(f"  Saved trades_sample.json (param={p_name})")
        break
