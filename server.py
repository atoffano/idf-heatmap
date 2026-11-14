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
from fastapi import FastAPI, HTTPException, Query, Request, Response
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

SEEDS: dict[str, dict[str, Any]] = {
    "sorbonne": {
        "id": "sorbonne",
        "name": "Sorbonne Université (Jussieu)",
        "address": "Campus Pierre et Marie Curie, 7-9 quai Saint Bernard, 75005 Paris",
        "lat": 48.8471,
        "lon": 2.3574,
        "badge": "SU",
    },
    "antony": {
        "id": "antony",
        "name": "Antony (Renaissance)",
        "address": "8 rue de la renaissance, 92160 Antony",
        "lat": 48.760967,
        "lon": 2.299217,
        "badge": "AT",
    },
}
DEFAULT_SEED = "sorbonne"

ORIGIN_LAT = SEEDS[DEFAULT_SEED]["lat"]
ORIGIN_LON = SEEDS[DEFAULT_SEED]["lon"]
METERS_PER_DEG_LAT = 111195.0
METERS_PER_DEG_LON = 111195.0 * math.cos(math.radians(ORIGIN_LAT))

ALLOWED_MODES = frozenset({"walk", "bike", "transit", "best"})
ALLOWED_STATION_FILTERS = frozenset({"all", "rail", "subway", "tram", "bus"})
ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD"})

RATE_LIMIT_MAX_REQUESTS = 600
RATE_LIMIT_WINDOW_SEC = 60.0

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://*.tile.openstreetmap.fr; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self';"
)


def _latlon_to_xy(
    lats: np.ndarray,
    lons: np.ndarray,
    orig_lat: float = ORIGIN_LAT,
    orig_lon: float = ORIGIN_LON,
) -> np.ndarray:
    meters_per_deg_lon = 111195.0 * math.cos(math.radians(orig_lat))
    x = (lons - orig_lon) * meters_per_deg_lon
    y = (lats - orig_lat) * METERS_PER_DEG_LAT
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


class SeedData:
    """Precomputed artifacts and spatial KDTree indices for a specific origin seed."""

    def __init__(
        self,
        seed_id: str,
        meta: dict[str, Any],
        grids: dict[str, Any],
        isochrones: dict[str, Any],
        stations: list[dict[str, Any]],
        sampled_crossroads: list[dict[str, Any]],
        df_crossroads: pd.DataFrame,
    ) -> None:
        self.seed_id = seed_id
        self.meta = meta
        self.grids = grids
        self.isochrones = isochrones
        self.stations = stations
        self.sampled_crossroads = sampled_crossroads
        self.df_crossroads = df_crossroads

        orig_lat = float(meta.get("origin_lat", ORIGIN_LAT))
        orig_lon = float(meta.get("origin_lon", ORIGIN_LON))
        self.orig_lat = orig_lat
        self.orig_lon = orig_lon

        cr_xy = _latlon_to_xy(
            df_crossroads["lat"].to_numpy(dtype=np.float64),
            df_crossroads["lon"].to_numpy(dtype=np.float64),
            orig_lat,
            orig_lon,
        )
        self.cr_tree = cKDTree(cr_xy)

        self.station_tree: cKDTree | None = None
        self.rail_stations: list[dict[str, Any]] = []
        self.rail_tree: cKDTree | None = None

        if stations:
            st_lats = np.array([s["lat"] for s in stations], dtype=np.float64)
            st_lons = np.array([s["lon"] for s in stations], dtype=np.float64)
            self.station_tree = cKDTree(_latlon_to_xy(st_lats, st_lons, orig_lat, orig_lon))

            self.rail_stations = [
                s for s in stations if s.get("mode") in {"train", "light_rail", "subway", "tram"}
            ]
            if self.rail_stations:
                r_lats = np.array([s["lat"] for s in self.rail_stations], dtype=np.float64)
                r_lons = np.array([s["lon"] for s in self.rail_stations], dtype=np.float64)
                self.rail_tree = cKDTree(_latlon_to_xy(r_lats, r_lons, orig_lat, orig_lon))


class AppState:
    """In-memory read-only spatial index and precomputed heatmap artifacts."""

    def __init__(self) -> None:
        self.loaded = False
        self.seeds_list: list[dict[str, Any]] = []
        self.seeds_data: dict[str, SeedData] = {}
        self.transit_lines: dict[str, Any] = {"type": "FeatureCollection", "features": []}
        self.bike_lanes: dict[str, Any] = {"type": "FeatureCollection", "features": []}

    def get_seed(self, seed_id: str | None = None) -> SeedData:
        if not self.loaded:
            raise HTTPException(status_code=503, detail="Data index not loaded")
        if seed_id is None:
            return self.seeds_data[DEFAULT_SEED]
        sid = seed_id.strip().lower()
        if sid not in self.seeds_data:
            raise HTTPException(status_code=400, detail=f"Invalid origin seed '{seed_id}'")
        return self.seeds_data[sid]

    @property
    def meta(self) -> dict[str, Any]:
        return self.get_seed(DEFAULT_SEED).meta

    @property
    def grids(self) -> dict[str, Any]:
        return self.get_seed(DEFAULT_SEED).grids

    @property
    def isochrones(self) -> dict[str, Any]:
        return self.get_seed(DEFAULT_SEED).isochrones

    @property
    def stations(self) -> list[dict[str, Any]]:
        return self.get_seed(DEFAULT_SEED).stations

    @property
    def sampled_crossroads(self) -> list[dict[str, Any]]:
        return self.get_seed(DEFAULT_SEED).sampled_crossroads

    @property
    def df_crossroads(self) -> pd.DataFrame:
        return self.get_seed(DEFAULT_SEED).df_crossroads

    @property
    def cr_tree(self) -> cKDTree:
        return self.get_seed(DEFAULT_SEED).cr_tree

    @property
    def station_tree(self) -> cKDTree | None:
        return self.get_seed(DEFAULT_SEED).station_tree

    @property
    def rail_stations(self) -> list[dict[str, Any]]:
        return self.get_seed(DEFAULT_SEED).rail_stations

    @property
    def rail_tree(self) -> cKDTree | None:
        return self.get_seed(DEFAULT_SEED).rail_tree

    def load(self) -> None:
        seeds_path = DATA_DIR / "seeds.json"
        if seeds_path.exists():
            with seeds_path.open("r", encoding="utf-8") as fh:
                self.seeds_list = json.load(fh)
        else:
            self.seeds_list = list(SEEDS.values())

        transit_lines_path = DATA_DIR / "transit_lines.json"
        bike_lanes_path = DATA_DIR / "bike_lanes.json"
        if transit_lines_path.exists():
            with transit_lines_path.open("r", encoding="utf-8") as fh:
                self.transit_lines = json.load(fh)
        if bike_lanes_path.exists():
            with bike_lanes_path.open("r", encoding="utf-8") as fh:
                self.bike_lanes = json.load(fh)

        loaded_seeds = 0
        for s in self.seeds_list:
            sid = s["id"]
            meta_path = DATA_DIR / f"metadata_{sid}.json"
            grids_path = DATA_DIR / f"heatmap_grids_{sid}.json"
            iso_path = DATA_DIR / f"isochrones_{sid}.json"
            stations_path = DATA_DIR / f"stations_{sid}.json"
            sampled_path = DATA_DIR / f"sampled_crossroads_{sid}.json"
            parquet_path = DATA_DIR / f"crossroads_travel_times_{sid}.parquet"

            # Fallbacks for default seed
            if not meta_path.exists() and sid == DEFAULT_SEED:
                meta_path = DATA_DIR / "metadata.json"
            if not grids_path.exists() and sid == DEFAULT_SEED:
                grids_path = DATA_DIR / "heatmap_grids.json"
            if not iso_path.exists() and sid == DEFAULT_SEED:
                iso_path = DATA_DIR / "isochrones.json"
            if not stations_path.exists() and sid == DEFAULT_SEED:
                stations_path = DATA_DIR / "stations.json"
            if not sampled_path.exists() and sid == DEFAULT_SEED:
                sampled_path = DATA_DIR / "sampled_crossroads.json"
            if not parquet_path.exists() and sid == DEFAULT_SEED:
                parquet_path = DATA_DIR / "crossroads_travel_times.parquet"

            req = [meta_path, grids_path, iso_path, stations_path, sampled_path, parquet_path]
            missing = [str(p) for p in req if not p.exists()]
            if missing:
                if sid == DEFAULT_SEED:
                    raise FileNotFoundError(f"Missing default seed artifacts: {missing}")
                logger.warning("Skipping seed '%s' due to missing artifacts: %s", sid, missing)
                continue

            with meta_path.open("r", encoding="utf-8") as fh:
                meta = json.load(fh)
            with grids_path.open("r", encoding="utf-8") as fh:
                grids = json.load(fh)
            with iso_path.open("r", encoding="utf-8") as fh:
                iso = json.load(fh)
            with stations_path.open("r", encoding="utf-8") as fh:
                st = json.load(fh)
            with sampled_path.open("r", encoding="utf-8") as fh:
                sampled = json.load(fh)
            df_cr = pd.read_parquet(parquet_path)

            self.seeds_data[sid] = SeedData(
                seed_id=sid,
                meta=meta,
                grids=grids,
                isochrones=iso,
                stations=st,
                sampled_crossroads=sampled,
                df_crossroads=df_cr,
            )
            loaded_seeds += 1

        if not self.seeds_data:
            raise FileNotFoundError("No seed datasets could be loaded from data dir.")

        self.loaded = True
        logger.info(
            "Loaded %d origin seeds into spatial index (%s)",
            loaded_seeds,
            list(self.seeds_data.keys()),
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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    else:
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


@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index(request: Request) -> Response:
    index_path = (STATIC_DIR / "index.html").resolve()
    if not str(index_path).startswith(str(STATIC_DIR.resolve()) + "/"):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    if request.method == "HEAD":
        return Response(
            content=b"",
            media_type="text/html; charset=utf-8",
            headers={"Content-Length": str(index_path.stat().st_size)},
        )
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "loaded": state.loaded, "seeds": [s["id"] for s in state.seeds_list]}


@app.get("/api/seeds")
async def get_seeds() -> list[dict[str, Any]]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    return state.seeds_list


@app.get("/api/meta")
async def get_meta(seed: str | None = Query(None, description="Origin seed id")) -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    sdata = state.get_seed(seed)
    return {
        **sdata.meta,
        "seeds": state.seeds_list,
        "server_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/api/grid/{mode}")
async def get_grid(
    mode: str,
    seed: str | None = Query(None, description="Origin seed id"),
) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Unsupported transport mode")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    sdata = state.get_seed(seed)
    values = sdata.grids.get("modes", {}).get(mode)
    if values is None:
        raise HTTPException(status_code=404, detail="Grid not available for mode")
    return {
        "bbox": sdata.grids["bbox"],
        "rows": sdata.grids["rows"],
        "cols": sdata.grids["cols"],
        "lats": sdata.grids["lats"],
        "lons": sdata.grids["lons"],
        "mode": mode,
        "seed": sdata.seed_id,
        "values": values,
    }


@app.get("/api/isochrones/{mode}")
async def get_isochrones(
    mode: str,
    seed: str | None = Query(None, description="Origin seed id"),
) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Unsupported transport mode")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    sdata = state.get_seed(seed)
    fc = sdata.isochrones.get(mode)
    if fc is None:
        raise HTTPException(status_code=404, detail="Isochrones not available for mode")
    return fc


@app.get("/api/stations")
async def get_stations(
    filter_mode: str = Query("all", alias="mode"),
    seed: str | None = Query(None, description="Origin seed id"),
) -> dict[str, Any]:
    if filter_mode not in ALLOWED_STATION_FILTERS:
        raise HTTPException(status_code=400, detail="Invalid station filter")
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")

    sdata = state.get_seed(seed)
    if filter_mode == "all":
        items = sdata.stations
    elif filter_mode == "rail":
        items = sdata.rail_stations
    else:
        items = [s for s in sdata.stations if s.get("mode") == filter_mode]

    return {"count": len(items), "seed": sdata.seed_id, "stations": items}


@app.get("/api/crossroads")
async def get_crossroads(
    seed: str | None = Query(None, description="Origin seed id"),
) -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    sdata = state.get_seed(seed)
    return {
        "seed": sdata.seed_id,
        "total_computed": len(sdata.df_crossroads) if sdata.df_crossroads is not None else 0,
        "sampled_count": len(sdata.sampled_crossroads),
        "crossroads": sdata.sampled_crossroads,
    }


@app.get("/api/transit-lines")
async def get_transit_lines() -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    return state.transit_lines


@app.get("/api/bike-lanes")
async def get_bike_lanes() -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")
    return state.bike_lanes


@app.get("/api/query")
async def query_point(
    lat: float = Query(..., ge=48.60, le=49.00, description="Latitude in WGS84"),
    lon: float = Query(..., ge=2.10, le=2.55, description="Longitude in WGS84"),
    seed: str | None = Query(None, description="Origin seed id"),
) -> dict[str, Any]:
    if not state.loaded:
        raise HTTPException(status_code=503, detail="Data index not loaded")

    sdata = state.get_seed(seed)
    if sdata.cr_tree is None or sdata.df_crossroads is None:
        raise HTTPException(status_code=503, detail="Data index not loaded")

    q_xy = _latlon_to_xy(np.array([lat]), np.array([lon]), sdata.orig_lat, sdata.orig_lon)[0]
    snap_dist_m, idx = sdata.cr_tree.query(q_xy)
    row = sdata.df_crossroads.iloc[int(idx)]

    nearest_station = None
    optimal_exit_station = None
    if sdata.station_tree is not None and sdata.stations:
        st_dist_m, st_idx = sdata.station_tree.query(q_xy)
        st = sdata.stations[int(st_idx)]
        st_d = float(st_dist_m)
        nearest_station = {
            **st,
            "distance_m": round(st_d, 1),
            "walk_from_station_min": round((st_d * 1.25) / 80.0, 1),
        }

        k_cand = min(15, len(sdata.stations))
        cand_dists, cand_idxs = sdata.station_tree.query(q_xy, k=k_cand)
        best_total = float("inf")
        best_cand: dict[str, Any] | None = None
        for d_val, i_val in zip(np.atleast_1d(cand_dists), np.atleast_1d(cand_idxs)):
            cand_st = sdata.stations[int(i_val)]
            cand_d = float(d_val)
            walk_min = (cand_d * 1.25) / 80.0
            total_min = float(cand_st["transit_min"]) + walk_min
            if total_min < best_total:
                best_total = total_min
                best_cand = {
                    **cand_st,
                    "distance_m": round(cand_d, 1),
                    "walk_from_station_min": round(walk_min, 1),
                    "estimated_total_min": round(total_min, 1),
                }
        optimal_exit_station = best_cand

    nearest_rail = None
    if sdata.rail_tree is not None and sdata.rail_stations:
        r_dist_m, r_idx = sdata.rail_tree.query(q_xy)
        rst = sdata.rail_stations[int(r_idx)]
        r_d = float(r_dist_m)
        nearest_rail = {
            **rst,
            "distance_m": round(r_d, 1),
            "walk_from_station_min": round((r_d * 1.25) / 80.0, 1),
        }

    return {
        "seed": sdata.seed_id,
        "query_lat": round(lat, 6),
        "query_lon": round(lon, 6),
        "snapped_lat": round(float(row["lat"]), 6),
        "snapped_lon": round(float(row["lon"]), 6),
        "snap_distance_m": round(float(snap_dist_m), 1),
        "distance_from_origin_km": round(
            _haversine_km(sdata.orig_lat, sdata.orig_lon, float(row["lat"]), float(row["lon"])),
            2,
        ),
        "walk_min": round(float(row["walk_min"]), 1),
        "bike_min": round(float(row["bike_min"]), 1),
        "transit_min": round(float(row["transit_min"]), 1),
        "best_min": round(float(row["best_min"]), 1),
        "fastest_mode": str(row["fastest_mode"]),
        "nearest_station": nearest_station,
        "nearest_rail_station": nearest_rail,
        "optimal_exit_station": optimal_exit_station,
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

