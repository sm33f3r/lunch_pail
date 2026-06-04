# Lunch Pail

NFL game outcome prediction engine for Polymarket.

## Stack
- Python 3.11+
- PostgreSQL
- XGBoost
- nflreadpy / nflverse

## Setup
1. Clone the repo
2. Create and activate a virtual environment
3. `pip install -r requirements.txt`
4. Configure `.env` with PostgreSQL credentials
5. Run `python ingest.py` to begin data ingestion

## Structure
- `ingest.py` — data ingestion pipeline
- `engineer.py` — feature engineering
- `train.py` — model training
- `calibrate.py` — probability calibration
- `predict.py` — game week predictions
- `report.py` — report generation
- `edge.py` — Polymarket edge calculation
- `scripts/` — utility scripts
- `reports/` — prediction report output