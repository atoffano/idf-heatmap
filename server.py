"""FastAPI backend for the Sorbonne Université (Jussieu) Transport Heatmap web app."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from scipy.spatial import cKDTree

# TODO(security): Public read-only transport visualization app; add OAuth2/JWT +
# RBAC if private user routes or saved profiles are introduced.
# TODO(security): No SQL/relational database used (read-only Parquet/JSON artifacts
# loaded in memory); use parameterized queries/ORM if database persistence is added.
# TODO(security): No file upload endpoints exist in this read-only application.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path("/mnt/data/sorbonne-transport-map")

ORIGIN_LAT = 48.8471
ORIGIN_LON = 2.3574
METERS_PER_DEG_LAT = 111195.0
METERS_PER_DEG_LON = 111195.0 * math.cos(math.radians(ORIGIN_LAT))

ALLOWED_MODES = frozenset({"walk", "bike", "transit", "best"})
ALLOWED_STATION_FILTERS = frozenset({"all", "rail", "subway", "tram", "bus"})
ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD"})

RATE_LIMIT_MAX_REQUESTS = 240
RATE_LIMIT_WINDOW_SEC = 60.0

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: https://*.basemaps.cartocdn.com https://tile.openstreetmap.org; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self';"
)


def _latlon_to_xy(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    x = (lons - ORIGIN_LON) * METERS_PER_DEG_LON
    y = (lats - ORIGIN_LAT) * METERS_PER_DEG_LAT
    return np.column_stack([x, y])


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi * 0.5) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda * 0.5) ** 2
    )
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


class AppState:
    """In-memory read-only spatial index and precomputed heatmap artifacts."""

    def __init__(self) -> None:
        self.loaded = False
        self.meta: dict[str, Any] = {}
        self.grids: dict[str, Any] = {}
        self.isochrones: dict[str, Any] = {}
        self.stations: list[dict[str, Any]] = []
        self.sampled_crossroads: list[dict[str, Any]] = []
        self.df_crossroads: pd.DataFrame | None = None
        self.cr_tree: cKDTree | None = None
        self.station_tree: cKDTree | None = None
        self.rail_stations: list[dict[str, Any]] = []
        self.rail_tree: cKDTree | None = None

    def load(self) -> None:
        meta_path = DATA_DIR / "metadata.json"
        grids_path = DATA_DIR / "heatmap_grids.json"
        iso_path = DATA_DIR / "isochrones.json"
        stations_path = DATA_DIR / "stations.json"
        sampled_path = DATA_DIR / "sampled_crossroads.json"
        parquet_path = DATA_DIR / "crossroads_travel_times.parquet"

        required = [
            meta_path,
            grids_path,
            iso_path,
            stations_path,
            sampled_path,
            parquet_path,
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise FileNotFoundError(
                f"Precomputed network artifacts missing ({missing}). Run build_network.py first."
            )

        with meta_path.open("r", encoding="utf-8") as fh:
            self.meta = json.load(fh)
        with grids_path.open("r", encoding="utf-8") as fh:
            self.grids = json.load(fh)
        with iso_path.open("r", encoding="utf-8") as fh:
            self.isochrones = json.load(fh)
        with stations_path.open("r", encoding="utf-8") as fh:
            self.stations = json.load(fh)
        with sampled_path.open("r", encoding="utf-8") as fh:
            self.sampled_crossroads = json.load(fh)

        self.df_crossroads = pd.read_parquet(parquet_path)
        cr_xy = _latlon_to_xy(
            self.df_crossroads["lat"].to_numpy(dtype=np.float64),
            self.df_crossroads["lon"].to_numpy(dtype=np.float64),
        )
        self.cr_tree = cKDTree(cr_xy)

        if self.stations:
            st_lats = np.array([s["lat"] for s in self.stations], dtype=np.float64)
            st_lons = np.array([s["lon"] for s in self.stations], dtype=np.float64)
            self.station_tree = cKDTree(_latlon_to_xy(st_lats, st_lons))

            self.rail_stations = [
                s for s in self.stations if s.get("mode") in {"train", "light_rail", "subway", "tram"}
            ]
            if self.rail_stations:
                r_lats = np.array([s["lat"] for s in self.rail_stations], dtype=np.float64)
                r_lons = np.array([s["lon"] for s in self.rail_stations], dtype=np.float64)
                self.rail_tree = cKDTree(_latlon_to_xy(r_lats, r_lons))

        self.loaded = True
        logger.info(
            "Loaded %d crossroads and %d transit stations into spatial index",
            len(self.df_crossroads),
            len(self.stations),
        )


state = AppState()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.load()
    yield


app = FastAPI(
    title="Sorbonne Université Transport Heatmap API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def security_and_rate_limit_middleware(request: Request, call_next):
    # 1. Enforce strict HTTP method allow-list
    if request.method not in ALLOWED_HTTP_METHODS:
        return JSONResponse(
            status_code=405,
            content={"detail": "Method not allowed"},
            headers={"Allow": "GET, HEAD"},
        )

    # 2. Sliding-window per-client IP rate limit
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[client_ip]
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please retry shortly."},
        )
    bucket.append(now)

    response = await call_next(request)

    # 3. Mandatory hardening security headers
    response.headers["Content-Security-Policy"] = CSP_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


@app.get("/")
async def serve_index() -> FileResponse:
    index_path = (STATIC_DIR / "index.html").resolve()
    if not str(index_path).startswith(str(STATIC_DIR.resolve()) + "/"):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


@app.get("/api/meta")
async def get_meta() -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    return {
        **state.meta,
        "server_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/api/grid/{mode}")
async def get_grid(mode: str) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Unsupported transport mode")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    values = state.grids.get("modes", {}).get(mode)
    if values is None:
        raise HTTPException(status_code=404, detail="Grid not available for mode")
    return {
        "bbox": state.grids["bbox"],
        "rows": state.grids["rows"],
        "cols": state.grids["cols"],
        "lats": state.grids["lats"],
        "lons": state.grids["lons"],
        "mode": mode,
        "values": values,
    }


@app.get("/api/isochrones/{mode}")
async def get_isochrones(mode: str) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Unsupported transport mode")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    fc = state.isochrones.get(mode)
    if fc is None:
        raise HTTPException(status_code=404, detail="Isochrones not available for mode")
    return fc


@app.get("/api/stations")
async def get_stations(
    filter_mode: str = Query("all", alias="mode"),
) -> dict[str, Any]:
    if filter_mode not in ALLOWED_STATION_FILTERS:
        raise HTTPException(status_code=400, detail="Invalid station filter")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")

    if filter_mode == "all":
        items = state.stations
    elif filter_mode == "rail":
        items = [s for s in state.stations if s.get("mode") in {"train", "light_rail", "subway", "tram"}]
    else:
        items = [s for s in state.stations if s.get("mode") == filter_mode]

    return {"count": len(items), "stations": items}


@app.get("/api/crossroads")
async def get_crossroads() -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    return {
        "total_computed": len(state.df_crossroads) if state.df_crossroads is not None else 0,
        "sampled_count": len(state.sampled_crossroads),
        "crossroads": state.sampled_crossroads,
    }


@app.get("/api/query")
async def query_point(
    lat: float = Query(..., ge=48.70, le=49.00, description="Latitude in WGS84"),
    lon: float = Query(..., ge=2.15, le=2.55, description="Longitude in WGS84"),
) -> dict[str, Any]:
    if not state.loaded or state.cr_tree is None or state.df_crossroads is None:
        raise HTTPException(status_code=503, detail="Data index not loaded")

    q_xy = _latlon_to_xy(np.array([lat]), np.array([lon]))[0]
    snap_dist_m, idx = state.cr_tree.query(q_xy)
    row = state.df_crossroads.iloc[int(idx)]

    nearest_station = None
    if state.station_tree is not None and state.stations:
        st_dist_m, st_idx = state.station_tree.query(q_xy)
        st = state.stations[int(st_idx)]
        nearest_station = {**st, "distance_m": round(float(st_dist_m), 1)}

    nearest_rail = None
    if state.rail_tree is not None and state.rail_stations:
        r_dist_m, r_idx = state.rail_tree.query(q_xy)
        rst = state.rail_stations[int(r_idx)]
        nearest_rail = {**rst, "distance_m": round(float(r_dist_m), 1)}

    return {
        "query_lat": round(lat, 6),
        "query_lon": round(lon, 6),
        "snapped_lat": round(float(row["lat"]), 6),
        "snapped_lon": round(float(row["lon"]), 6),
        "snap_distance_m": round(float(snap_dist_m), 1),
        "distance_from_origin_km": round(
            _haversine_km(ORIGIN_LAT, ORIGIN_LON, float(row["lat"]), float(row["lon"])),
            2,
        ),
        "walk_min": round(float(row["walk_min"]), 1),
        "bike_min": round(float(row["bike_min"]), 1),
        "transit_min": round(float(row["transit_min"]), 1),
        "best_min": round(float(row["best_min"]), 1),
        "fastest_mode": str(row["fastest_mode"]),
        "nearest_station": nearest_station,
        "nearest_rail_station": nearest_rail,
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

