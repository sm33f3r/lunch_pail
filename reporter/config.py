"""
reporter/config.py

Loads and validates all reporter configuration from environment variables.
Fails loudly at import time if any trading-relevant variable is missing or invalid.

Load order: environment → .env file in reporter/ directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_REPORTER_DIR = Path(__file__).resolve().parent
load_dotenv(_REPORTER_DIR / ".env")

VALID_VOLUME_WINDOWS = {"24hr", "1wk", "1mo", "1yr", "lifetime"}


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(
            f"[reporter.config] Required environment variable '{name}' is not set. "
            f"See reporter/.env.example for documentation."
        )
    return val


def _require_float(name: str) -> float:
    raw = _require(name)
    try:
        return float(raw)
    except ValueError:
        raise EnvironmentError(
            f"[reporter.config] '{name}' must be a float (got: {raw!r})."
        )


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError:
        raise EnvironmentError(
            f"[reporter.config] '{name}' must be an integer (got: {raw!r})."
        )


def _require_path(name: str) -> Path:
    raw = _require(name)
    return Path(raw)


def _require_volume_window(name: str) -> str:
    raw = _require(name)
    if raw not in VALID_VOLUME_WINDOWS:
        raise EnvironmentError(
            f"[reporter.config] '{name}' must be one of {sorted(VALID_VOLUME_WINDOWS)} "
            f"(got: {raw!r})."
        )
    return raw


def _optional_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # API base URLs — have well-known defaults; override for testing/staging
    gamma_api_base_url: str
    data_api_base_url: str
    clob_api_base_url: str

    # Polymarket NFL tag — confirmed by recon; override only if Polymarket reassigns it
    nfl_tag_id: int

    # Trading-relevant — required, no silent defaults
    volume_threshold: float
    volume_window: str
    report_output_dir: Path
    polling_interval_seconds: int


def _load() -> Settings:
    return Settings(
        gamma_api_base_url=_optional_str(
            "GAMMA_API_BASE_URL", "https://gamma-api.polymarket.com"
        ),
        data_api_base_url=_optional_str(
            "DATA_API_BASE_URL", "https://data-api.polymarket.com"
        ),
        clob_api_base_url=_optional_str(
            "CLOB_API_BASE_URL", "https://clob.polymarket.com"
        ),
        nfl_tag_id=int(_optional_str("NFL_TAG_ID", "450")),
        volume_threshold=_require_float("VOLUME_THRESHOLD"),
        volume_window=_require_volume_window("VOLUME_WINDOW"),
        report_output_dir=_require_path("REPORT_OUTPUT_DIR"),
        polling_interval_seconds=_require_int("POLLING_INTERVAL_SECONDS"),
    )


settings = _load()
