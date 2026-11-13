"""Optional external routing providers for calibrating station travel times.

Supports:
- Google Maps Routes API (`computeRouteMatrix` with TRANSIT mode)
- Île-de-France Mobilités PRIM / Navitia API (`journeys` endpoint)
- Citymapper Travel Time API (optional)

All API keys are read strictly from environment variables (`.env`) and never
hardcoded or logged. When no external keys are configured, the pipeline runs
100% on open-source OpenStreetMap + IDFM Open Data.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GOOGLE_ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
IDFM_PRIM_JOURNEYS_URL = "https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia/journeys"
CITYMAPPER_TRAVELTIME_URL = "https://api.external.citymapper.com/api/1/traveltimes"

REQUEST_TIMEOUT_SEC = 15


def get_configured_providers() -> dict[str, bool]:
    """Return which optional external API providers have keys configured."""
    return {
        "google_maps": bool(os.getenv("GOOGLE_MAPS_API_KEY", "").strip()),
        "idfm_prim": bool(os.getenv("IDFM_PRIM_API_KEY", "").strip()),
        "citymapper": bool(os.getenv("CITYMAPPER_API_KEY", "").strip()),
    }


def _parse_google_duration_seconds(duration_str: str | None) -> float | None:
    """Convert Google Routes duration strings like '915s' into float seconds."""
    if not duration_str or not isinstance(duration_str, str):
        return None
    stripped = duration_str.strip()
    if not stripped.endswith("s"):
        return None
    try:
        return float(stripped[:-1])
    except ValueError:
        return None


def fetch_google_station_times(
    origin_lat: float,
    origin_lon: float,
    stations: list[dict[str, Any]],
    batch_size: int = 25,
) -> dict[str, float]:
    """Fetch transit travel times (seconds) from origin to stations via Google Routes API.

    Returns a mapping `{station_id: duration_sec}` for stations successfully resolved.
    """
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key or not stations:
        return {}

    results: dict[str, float] = {}
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition,status",
    }
    origin_waypoint = {
        "waypoint": {
            "location": {
                "latLng": {"latitude": origin_lat, "longitude": origin_lon}
            }
        }
    }

    for start in range(0, len(stations), batch_size):
        chunk = stations[start : start + batch_size]
        payload = {
            "origins": [origin_waypoint],
            "destinations": [
                {
                    "waypoint": {
                        "location": {
                            "latLng": {
                                "latitude": float(st["lat"]),
                                "longitude": float(st["lon"]),
                            }
                        }
                    }
                }
                for st in chunk
            ],
            "travelMode": "TRANSIT",
        }
        try:
            resp = requests.post(
                GOOGLE_ROUTES_MATRIX_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Google Routes Matrix request returned status %s for batch %s",
                    resp.status_code,
                    start,
                )
                continue
            entries = resp.json()
            if not isinstance(entries, list):
                continue
            for item in entries:
                if item.get("condition") != "ROUTE_EXISTS":
                    continue
                dest_idx = item.get("destinationIndex")
                sec = _parse_google_duration_seconds(item.get("duration"))
                if isinstance(dest_idx, int) and 0 <= dest_idx < len(chunk) and sec is not None:
                    results[str(chunk[dest_idx]["id"])] = sec
        except requests.RequestException as exc:
            logger.warning("Google Routes API batch %s failed: %s", start, type(exc).__name__)
        time.sleep(0.1)

    return results


def fetch_idfm_prim_station_times(
    origin_lat: float,
    origin_lon: float,
    stations: list[dict[str, Any]],
    max_requests: int = 120,
) -> dict[str, float]:
    """Fetch transit travel times (seconds) via IDFM PRIM Navitia journeys endpoint.

    Returns a mapping `{station_id: duration_sec}` for queried stations.
    """
    api_key = os.getenv("IDFM_PRIM_API_KEY", "").strip()
    if not api_key or not stations:
        return {}

    results: dict[str, float] = {}
    headers = {"apiKey": api_key, "Accept": "application/json"}
    from_coord = f"{origin_lon:.6f};{origin_lat:.6f}"

    for st in stations[:max_requests]:
        to_coord = f"{float(st['lon']):.6f};{float(st['lat']):.6f}"
        params = {
            "from": from_coord,
            "to": to_coord,
            "count": 1,
        }
        try:
            resp = requests.get(
                IDFM_PRIM_JOURNEYS_URL,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            journeys = data.get("journeys") or []
            if journeys and isinstance(journeys[0].get("duration"), (int, float)):
                results[str(st["id"])] = float(journeys[0]["duration"])
        except requests.RequestException:
            continue
        time.sleep(0.12)

    return results

