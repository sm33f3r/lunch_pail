"""Hit /sports and save raw response."""
import json, requests
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent

r = requests.get("https://gamma-api.polymarket.com/sports", timeout=15)
r.raise_for_status()
data = r.json()
print(f"Status: {r.status_code}  |  Rate-limit headers: {dict((k,v) for k,v in r.headers.items() if 'limit' in k.lower() or 'rate' in k.lower())}")
print(json.dumps(data, indent=2)[:3000])
(OUT / "sports_response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
print(f"\nSaved sports_response.json  ({len(data)} entries)")
