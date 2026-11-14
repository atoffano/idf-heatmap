"""Build multimodal transport network and travel-time heatmaps from Sorbonne Université.

Origin:
    Sorbonne Université — Campus Pierre et Marie Curie
    7-9 quai Saint Bernard, 75005 Paris (48.8471, 2.3574)

Pipeline:
1. Download & cache OpenStreetMap street/cycleway network and transit route relations
   via Overpass API (with custom User-Agent and mirror fallback), plus IDFM station data.
2. Construct walkable (`G_walk`), cyclable (`G_bike`), and multimodal transit (`G_transit`)
   graphs using NetworkX, identifying all street crossroads (intersections).
3. Run single-source Dijkstra from the campus origin across all three networks,
   optionally overriding hub station arrival times via external_providers.py when
   API keys are configured in `.env`.
4. Save all crossroad travel times to `/mnt/data/sorbonne-transport-map/crossroads_travel_times.parquet`.
5. Compute 200x200 metric IDW interpolated grids and GeoJSON isochrone contours
   (5, 10, 15, 20, 30, 45, 60 min) for `walk`, `bike`, `transit`, and `best`.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import contourpy
import networkx as nx
import numpy as np
import pandas as pd
import requests
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from build_overlays import generate_overlays
from external_providers import (
    fetch_google_station_times,
    fetch_idfm_prim_station_times,
    get_configured_providers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Configured origin seeds
SEEDS: dict[str, dict[str, Any]] = {
    "sorbonne": {
        "id": "sorbonne",
        "name": "Sorbonne Université (Jussieu)",
        "address": (
            "Sorbonne Université — Campus Pierre et Marie Curie, "
            "7-9 quai Saint Bernard, 75005 Paris"
        ),
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
ORIGIN_ADDRESS = SEEDS[DEFAULT_SEED]["address"]

# Paris + Petite Couronne + Antony (~18 km N-S x 17 km E-W)
# (south_lat, west_lon, north_lat, east_lon)
BBOX = (48.665, 2.155, 48.915, 2.480)
GRID_N = 200
ISOCHRONE_LEVELS = [5, 10, 15, 20, 30, 45, 60]

DATA_DIR = Path("/mnt/data/sorbonne-transport-map")
ROADS_CACHE = DATA_DIR / "raw_osm_roads.json"
TRANSIT_CACHE = DATA_DIR / "raw_osm_transit.json"
IDFM_STATIONS_CACHE = DATA_DIR / "raw_idfm_stations.json"

SEEDS_OUT = DATA_DIR / "seeds.json"
PARQUET_OUT = DATA_DIR / "crossroads_travel_times.parquet"
GRIDS_OUT = DATA_DIR / "heatmap_grids.json"
ISOCHRONES_OUT = DATA_DIR / "isochrones.json"
STATIONS_OUT = DATA_DIR / "stations.json"
SAMPLED_CROSSROADS_OUT = DATA_DIR / "sampled_crossroads.json"
META_OUT = DATA_DIR / "metadata.json"

OVERPASS_ENDPOINTS = [
    "https://ovp.dresel.systems/api/interpreter",
    "https://overpass.maprva.org/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "SorbonneTransportMap/1.0 (https://sorbonne-universite.fr)"

IDFM_STATIONS_URL = (
    "https://data.iledefrance-mobilites.fr/api/explore/v2.1/"
    "catalog/datasets/emplacement-des-gares-idf/exports/json"
)

# Metric projection factors centered on Paris
METERS_PER_DEG_LAT = 111195.0
METERS_PER_DEG_LON = 111195.0 * math.cos(math.radians(ORIGIN_LAT))

# Transit physics parameters
TRANSIT_PARAMS = {
    "train": {"access_wait_sec": 210.0, "egress_sec": 60.0, "speed_ms": 11.67, "dwell_sec": 22.0, "label": "RER / Transilien"},
    "light_rail": {"access_wait_sec": 210.0, "egress_sec": 60.0, "speed_ms": 11.67, "dwell_sec": 22.0, "label": "RER / Light Rail"},
    "subway": {"access_wait_sec": 150.0, "egress_sec": 60.0, "speed_ms": 7.50, "dwell_sec": 18.0, "label": "Métro"},
    "tram": {"access_wait_sec": 160.0, "egress_sec": 15.0, "speed_ms": 5.28, "dwell_sec": 16.0, "label": "Tramway"},
    "bus": {"access_wait_sec": 195.0, "egress_sec": 15.0, "speed_ms": 3.75, "dwell_sec": 14.0, "label": "Bus"},
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in meters between two WGS84 points."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi * 0.5) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda * 0.5) ** 2
    )
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def latlon_to_xy(
    lats: np.ndarray,
    lons: np.ndarray,
    orig_lat: float = ORIGIN_LAT,
    orig_lon: float = ORIGIN_LON,
) -> np.ndarray:
    """Project lat/lon arrays into local metric (x_m, y_m) relative to origin."""
    meters_per_deg_lon = 111195.0 * math.cos(math.radians(orig_lat))
    x = (lons - orig_lon) * meters_per_deg_lon
    y = (lats - orig_lat) * METERS_PER_DEG_LAT
    return np.column_stack([x, y])


def query_overpass(query: str, cache_path: Path, force_refresh: bool = False) -> dict[str, Any]:
    """Query Overpass API with mirror fallback and local JSON caching."""
    if cache_path.exists() and not force_refresh:
        logger.info("Loading cached Overpass data from %s", cache_path)
        with cache_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("elements"):
            return data

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    last_err: Exception | None = None
    for attempt in range(3):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                logger.info("Querying %s (attempt %d)...", endpoint, attempt + 1)
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    headers=headers,
                    timeout=180,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("elements"):
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp_path = cache_path.with_suffix(".json.tmp")
                        with tmp_path.open("w", encoding="utf-8") as fh:
                            json.dump(data, fh)
                        tmp_path.replace(cache_path)
                        logger.info(
                            "Saved %d Overpass elements to %s",
                            len(data["elements"]),
                            cache_path,
                        )
                        return data
                    logger.warning("Empty Overpass response from %s", endpoint)
                else:
                    logger.warning("Overpass %s returned HTTP %d", endpoint, resp.status_code)
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
                logger.warning("Overpass %s failed: %s", endpoint, exc)
        time.sleep(3 * (attempt + 1))

    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def fetch_idfm_stations(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch IDFM rail/metro/tram stations dataset for name/mode enrichment."""
    if IDFM_STATIONS_CACHE.exists() and not force_refresh:
        try:
            with IDFM_STATIONS_CACHE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                return data
        except (OSError, ValueError):
            pass

    try:
        resp = requests.get(
            IDFM_STATIONS_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                with IDFM_STATIONS_CACHE.open("w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                logger.info("Cached %d IDFM station records", len(data))
                return data
    except (requests.RequestException, ValueError) as exc:
        logger.warning("IDFM station metadata fetch skipped: %s", exc)
    return []


def snap_origin_to_graph(
    lat: float,
    lon: float,
    label: str,
    valid_nodes: dict[int, tuple[float, float]],
    nodes_coord: dict[int, tuple[float, float]],
    g_walk: nx.Graph,
    g_bike: nx.Graph,
) -> int:
    """Snap an origin coordinate to nearest street node and guarantee bicycle connectivity."""
    node_ids = np.fromiter(valid_nodes.keys(), dtype=np.int64)
    coords_arr = np.array([valid_nodes[nid] for nid in node_ids], dtype=np.float64)
    xy_arr = latlon_to_xy(coords_arr[:, 0], coords_arr[:, 1], orig_lat=lat, orig_lon=lon)
    tree = cKDTree(xy_arr)
    dist_orig, idx_orig = tree.query([0.0, 0.0])
    origin_node = int(node_ids[idx_orig])
    logger.info(
        "Snapped origin '%s' to street node %d (%.1fm from %.5f, %.5f)",
        label,
        origin_node,
        dist_orig,
        lat,
        lon,
    )

    if origin_node not in g_bike:
        bike_ids = np.fromiter(g_bike.nodes(), dtype=np.int64)
        bike_coords = np.array([nodes_coord[nid] for nid in bike_ids], dtype=np.float64)
        b_tree = cKDTree(latlon_to_xy(bike_coords[:, 0], bike_coords[:, 1], orig_lat=lat, orig_lon=lon))
        _, b_idx = b_tree.query([0.0, 0.0])
        bike_origin = int(bike_ids[b_idx])
        g_bike.add_edge(origin_node, bike_origin, weight=15.0, dist_m=20.0)

    return origin_node


def build_street_graphs(
    roads_data: dict[str, Any],
) -> tuple[nx.Graph, nx.Graph, dict[int, tuple[float, float]], set[int], dict[int, tuple[float, float]]]:
    """Construct G_walk and G_bike from Overpass road elements and detect crossroads."""
    s_lat, w_lon, n_lat, e_lon = BBOX
    nodes_coord: dict[int, tuple[float, float]] = {}
    ways: list[dict[str, Any]] = []

    for el in roads_data.get("elements", []):
        etype = el.get("type")
        if etype == "node":
            lat = el.get("lat")
            lon = el.get("lon")
            if lat is not None and lon is not None:
                nodes_coord[int(el["id"])] = (float(lat), float(lon))
        elif etype == "way" and el.get("nodes"):
            ways.append(el)

    logger.info("Parsed %d nodes and %d road ways", len(nodes_coord), len(ways))

    g_walk = nx.Graph()
    g_bike = nx.Graph()
    way_membership_count: dict[int, int] = defaultdict(int)

    for way in ways:
        tags = way.get("tags") or {}
        highway = tags.get("highway", "")
        access = tags.get("access", "")
        foot = tags.get("foot", "")
        bicycle = tags.get("bicycle", "")
        service = tags.get("service", "")

        # Skip strictly private ways unless public foot/bike access is tagged
        if access in {"private", "no"} and foot != "yes" and bicycle != "yes":
            continue
        if service == "parking_aisle":
            continue

        nds = [int(nid) for nid in way["nodes"] if int(nid) in nodes_coord]
        if len(nds) < 2:
            continue

        # Determine walk and bike speeds (m/s)
        if highway == "steps":
            walk_speed = 0.85
        else:
            walk_speed = 1.333  # 4.8 km/h

        if highway == "cycleway" or tags.get("cycleway") in {"track", "lane"}:
            bike_speed = 5.14  # 18.5 km/h
        elif highway in {"footway", "pedestrian", "steps"}:
            if bicycle == "yes" and highway != "steps":
                bike_speed = 3.00
            else:
                bike_speed = 1.25  # dismounted walk speed
        else:
            bike_speed = 4.30  # 15.5 km/h on Paris streets

        # Track unique ways passing through each node for crossroad detection
        seen_in_way = set()
        for nid in nds:
            if nid not in seen_in_way:
                way_membership_count[nid] += 1
                seen_in_way.add(nid)

        for i in range(len(nds) - 1):
            u = nds[i]
            v = nds[i + 1]
            if u == v:
                continue
            lat1, lon1 = nodes_coord[u]
            lat2, lon2 = nodes_coord[v]
            dist_m = haversine_m(lat1, lon1, lat2, lon2)
            if dist_m <= 0.0:
                continue

            walk_w = dist_m / walk_speed
            # Slight intersection/turn friction on bike segments
            bike_w = (dist_m / bike_speed) + (1.2 if highway not in {"cycleway"} else 0.4)

            if foot != "no":
                existing_w = g_walk.get_edge_data(u, v)
                if existing_w is None or walk_w < existing_w["weight"]:
                    g_walk.add_edge(u, v, weight=walk_w, dist_m=dist_m)

            if bicycle != "no":
                existing_b = g_bike.get_edge_data(u, v)
                if existing_b is None or bike_w < existing_b["weight"]:
                    g_bike.add_edge(u, v, weight=bike_w, dist_m=dist_m)

    if g_walk.number_of_nodes() == 0:
        raise RuntimeError("Walk graph is empty after parsing Overpass roads.")

    # Keep giant connected component of g_walk containing origin
    components = sorted(nx.connected_components(g_walk), key=len, reverse=True)
    giant_nodes = components[0]
    g_walk = g_walk.subgraph(giant_nodes).copy()
    g_bike = g_bike.subgraph(giant_nodes & set(g_bike.nodes())).copy()

    # Keep only coordinates in the giant connected component inside BBOX
    valid_nodes = {
        nid: nodes_coord[nid]
        for nid in giant_nodes
        if s_lat <= nodes_coord[nid][0] <= n_lat and w_lon <= nodes_coord[nid][1] <= e_lon
    }

    # Identify crossroads: degree >= 3 OR shared by >= 2 distinct OSM ways
    crossroad_nodes = {
        nid
        for nid in valid_nodes
        if g_walk.degree(nid) >= 3 or way_membership_count.get(nid, 0) >= 2
    }

    logger.info(
        "Built street graphs: walk=%d nodes / %d edges, bike=%d nodes / %d edges, crossroads=%d",
        g_walk.number_of_nodes(),
        g_walk.number_of_edges(),
        g_bike.number_of_nodes(),
        g_bike.number_of_edges(),
        len(crossroad_nodes),
    )
    return g_walk, g_bike, nodes_coord, crossroad_nodes, valid_nodes


def _extract_ordered_stops(
    rel: dict[str, Any], transit_nodes: dict[int, dict[str, Any]]
) -> list[int]:
    """Extract ordered stop node IDs from an OSM transit route relation."""
    members = rel.get("members") or []
    stop_candidates: list[int] = []
    platform_candidates: list[int] = []
    fallback_candidates: list[int] = []

    for m in members:
        if m.get("type") != "node":
            continue
        ref = int(m["ref"])
        if ref not in transit_nodes:
            continue
        role = (m.get("role") or "").lower()
        if "stop" in role:
            stop_candidates.append(ref)
        elif "platform" in role:
            platform_candidates.append(ref)
        else:
            fallback_candidates.append(ref)

    chosen = stop_candidates or platform_candidates or fallback_candidates
    # Deduplicate consecutive identical stop IDs
    deduped: list[int] = []
    for nid in chosen:
        if not deduped or deduped[-1] != nid:
            deduped.append(nid)
    return deduped


def _pick_station_mode(modes: set[str]) -> str:
    """Choose primary display mode for a multi-mode transit stop."""
    for priority in ("train", "light_rail", "subway", "tram", "bus"):
        if priority in modes:
            return priority
    return "bus"


def build_multimodal_transit_graph(
    g_walk: nx.Graph,
    nodes_coord: dict[int, tuple[float, float]],
    transit_data: dict[str, Any],
    origin_node: int | None = None,
) -> tuple[nx.DiGraph, list[dict[str, Any]]]:
    """Build directed multimodal transit graph combining G_walk and OSM transit lines."""
    g_transit = nx.DiGraph()

    # Copy undirected walk graph as bidirectional edges
    for u, v, attrs in g_walk.edges(data=True):
        w = attrs["weight"]
        g_transit.add_edge(u, v, weight=w)
        g_transit.add_edge(v, u, weight=w)

    # Street node KDTree for snapping transit stops within 250m
    street_ids = np.fromiter(g_walk.nodes(), dtype=np.int64)
    street_coords = np.array([nodes_coord[nid] for nid in street_ids], dtype=np.float64)
    street_xy = latlon_to_xy(street_coords[:, 0], street_coords[:, 1])
    street_tree = cKDTree(street_xy)

    # Parse transit nodes and relations
    t_nodes: dict[int, dict[str, Any]] = {}
    t_rels: list[dict[str, Any]] = []
    for el in transit_data.get("elements", []):
        if el.get("type") == "node" and el.get("lat") is not None and el.get("lon") is not None:
            t_nodes[int(el["id"])] = el
        elif el.get("type") == "relation":
            t_rels.append(el)

    logger.info("Parsed %d transit relations and %d stop nodes", len(t_rels), len(t_nodes))

    # Track route line refs per (ref, route_type) to identify paired directional relations
    refs_by_type: dict[tuple[str, str], int] = defaultdict(int)
    for rel in t_rels:
        tags = rel.get("tags") or {}
        r_type = tags.get("route", "")
        r_ref = tags.get("ref") or tags.get("name") or ""
        if r_type and r_ref:
            refs_by_type[(r_type, r_ref)] += 1

    # Track stop metadata so we can build clean stations.json
    stop_records: dict[int, dict[str, Any]] = {}
    s_lat, w_lon, n_lat, e_lon = BBOX
    route_edges_added = 0

    for rel in t_rels:
        tags = rel.get("tags") or {}
        r_type = tags.get("route", "")
        if r_type not in TRANSIT_PARAMS:
            continue
        params = TRANSIT_PARAMS[r_type]
        rel_id = int(rel["id"])
        line_ref = (tags.get("ref") or tags.get("name") or r_type).strip()
        rel_name = tags.get("name") or line_ref

        stop_ids = _extract_ordered_stops(rel, t_nodes)
        if len(stop_ids) < 2:
            continue

        # Snap stops inside/near street network
        snapped_indices: list[tuple[int, int, int, float]] = []
        stop_coords = np.array(
            [[float(t_nodes[sid]["lat"]), float(t_nodes[sid]["lon"])] for sid in stop_ids],
            dtype=np.float64,
        )
        stop_xy = latlon_to_xy(stop_coords[:, 0], stop_coords[:, 1])
        dists, idxs = street_tree.query(stop_xy)

        for idx_in_route, (sid, dist_m, street_idx) in enumerate(zip(stop_ids, dists, idxs)):
            if dist_m > 250.0:
                continue
            street_nid = int(street_ids[street_idx])
            onboard_node = ("tr", rel_id, idx_in_route)

            walk_snap_sec = float(dist_m) / 1.333
            # Board edge: street -> onboard
            g_transit.add_edge(
                street_nid,
                onboard_node,
                weight=params["access_wait_sec"] + walk_snap_sec,
            )
            # Alight edge: onboard -> street
            g_transit.add_edge(
                onboard_node,
                street_nid,
                weight=params["egress_sec"] + walk_snap_sec,
            )
            snapped_indices.append((idx_in_route, sid, street_nid, float(dist_m)))

            st_el = t_nodes[sid]
            st_lat = float(st_el["lat"])
            st_lon = float(st_el["lon"])
            if s_lat <= st_lat <= n_lat and w_lon <= st_lon <= e_lon:
                st_tags = st_el.get("tags") or {}
                st_name = (
                    st_tags.get("name")
                    or st_tags.get("uic_name")
                    or st_tags.get("ref")
                    or f"Stop {sid}"
                )
                rec = stop_records.setdefault(
                    sid,
                    {
                        "id": str(sid),
                        "name": st_name,
                        "lat": st_lat,
                        "lon": st_lon,
                        "modes": set(),
                        "lines": set(),
                        "onboard_nodes": [],
                    },
                )
                rec["modes"].add(r_type)
                if line_ref:
                    rec["lines"].add(line_ref)
                rec["onboard_nodes"].append(onboard_node)

        if len(snapped_indices) < 2:
            continue

        # Determine if relation is explicitly directional vs bidirectional
        is_directional = (
            ("→" in rel_name or "->" in rel_name or "=>" in rel_name)
            and refs_by_type.get((r_type, line_ref), 0) >= 2
            and tags.get("oneway") != "no"
        )

        # Link consecutive stops along the route
        for pos in range(len(snapped_indices) - 1):
            idx_a, sid_a, _, _ = snapped_indices[pos]
            idx_b, sid_b, _, _ = snapped_indices[pos + 1]
            # Only connect reasonably close stops (avoid jumping across outside-BBOX gaps)
            lat_a, lon_a = float(t_nodes[sid_a]["lat"]), float(t_nodes[sid_a]["lon"])
            lat_b, lon_b = float(t_nodes[sid_b]["lat"]), float(t_nodes[sid_b]["lon"])
            hop_m = haversine_m(lat_a, lon_a, lat_b, lon_b)
            if hop_m > 6000.0 or hop_m < 1.0:
                continue

            # Account for intermediate skipped stops if idx_b - idx_a > 1
            dwell_count = max(1, idx_b - idx_a)
            travel_sec = (hop_m / params["speed_ms"]) + params["dwell_sec"] * dwell_count

            u_node = ("tr", rel_id, idx_a)
            v_node = ("tr", rel_id, idx_b)
            g_transit.add_edge(u_node, v_node, weight=travel_sec)
            route_edges_added += 1
            if not is_directional:
                g_transit.add_edge(v_node, u_node, weight=travel_sec)
                route_edges_added += 1

    logger.info(
        "Multimodal transit graph ready: %d nodes, %d edges (%d transit hop edges), %d unique stops",
        g_transit.number_of_nodes(),
        g_transit.number_of_edges(),
        route_edges_added,
        len(stop_records),
    )

    # Optionally calibrate major stations via Google Routes / IDFM PRIM APIs
    providers = get_configured_providers()
    station_list_for_api = [
        {
            "id": rec["id"],
            "name": rec["name"],
            "lat": rec["lat"],
            "lon": rec["lon"],
            "mode": _pick_station_mode(rec["modes"]),
        }
        for rec in stop_records.values()
        if rec["modes"] & {"subway", "train", "light_rail"}
    ]
    # Deduplicate by name to minimize API calls if keys are provided
    seen_hub_names: set[str] = set()
    hub_stations: list[dict[str, Any]] = []
    for st in station_list_for_api:
        key = f"{st['mode']}:{st['name'].lower()}"
        if key not in seen_hub_names:
            seen_hub_names.add(key)
            hub_stations.append(st)

    calibrated_sec: dict[str, float] = {}
    if providers["google_maps"]:
        logger.info("Fetching live Google Routes API station calibration for %d hubs...", len(hub_stations))
        calibrated_sec.update(fetch_google_station_times(ORIGIN_LAT, ORIGIN_LON, hub_stations))
    elif providers["idfm_prim"]:
        logger.info("Fetching live IDFM PRIM station calibration...")
        calibrated_sec.update(fetch_idfm_prim_station_times(ORIGIN_LAT, ORIGIN_LON, hub_stations))

    if origin_node is not None and calibrated_sec:
        name_to_sec = {
            st["name"].lower(): calibrated_sec[st["id"]]
            for st in hub_stations
            if st["id"] in calibrated_sec
        }
        applied = 0
        for rec in stop_records.values():
            sec = name_to_sec.get(rec["name"].lower())
            if sec is not None and sec > 0:
                for ob_node in rec["onboard_nodes"]:
                    g_transit.add_edge(origin_node, ob_node, weight=sec)
                    applied += 1
        logger.info("Applied %d API-calibrated transit shortcut edges", applied)

    # Flatten stop records into clean station dicts (will populate transit_min after Dijkstra)
    raw_stations = list(stop_records.values())
    return g_transit, raw_stations


def deduplicate_stations(
    raw_stations: list[dict[str, Any]],
    transit_lengths: dict[Any, float],
    idfm_stations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate stop platforms sharing the same station name & mode into clean map stations."""
    # Compute travel time at each stop record from its onboard nodes
    clusters: dict[tuple[str, str], dict[str, Any]] = {}

    for rec in raw_stations:
        best_sec = min(
            (transit_lengths.get(ob, float("inf")) for ob in rec["onboard_nodes"]),
            default=float("inf"),
        )
        if not math.isfinite(best_sec):
            continue
        primary_mode = _pick_station_mode(rec["modes"])
        # Keep rail/metro/tram stations plus named bus stops clustered by rounded coords + name
        norm_name = rec["name"].strip()
        if primary_mode in {"train", "light_rail", "subway", "tram"}:
            cluster_key = (primary_mode, norm_name.lower())
        else:
            # Bus stops often have identical names on opposite corners; cluster within ~150m
            cluster_key = (
                "bus",
                f"{norm_name.lower()}_{round(rec['lat'], 3)}_{round(rec['lon'], 3)}",
            )

        existing = clusters.get(cluster_key)
        if existing is None:
            clusters[cluster_key] = {
                "id": rec["id"],
                "name": norm_name,
                "mode": primary_mode,
                "lines": set(rec["lines"]),
                "lats": [rec["lat"]],
                "lons": [rec["lon"]],
                "best_sec": best_sec,
            }
        else:
            existing["lines"].update(rec["lines"])
            existing["lats"].append(rec["lat"])
            existing["lons"].append(rec["lon"])
            if best_sec < existing["best_sec"]:
                existing["best_sec"] = best_sec

    stations_out: list[dict[str, Any]] = []
    for cl in clusters.values():
        mode = cl["mode"]
        mode_label = TRANSIT_PARAMS.get(mode, {}).get("label", mode.title())
        stations_out.append(
            {
                "id": cl["id"],
                "name": cl["name"],
                "mode": mode,
                "mode_label": mode_label,
                "lines": sorted(cl["lines"])[:12],
                "lat": round(float(np.mean(cl["lats"])), 6),
                "lon": round(float(np.mean(cl["lons"])), 6),
                "transit_min": round(cl["best_sec"] / 60.0, 2),
            }
        )

    # Sort so rail/metro/tram come first, then bus stops by travel time
    priority_order = {"train": 0, "light_rail": 1, "subway": 2, "tram": 3, "bus": 4}
    stations_out.sort(key=lambda s: (priority_order.get(s["mode"], 9), s["transit_min"]))
    logger.info(
        "Deduplicated to %d transit stations (%d rail/metro/tram, %d bus)",
        len(stations_out),
        sum(1 for s in stations_out if s["mode"] != "bus"),
        sum(1 for s in stations_out if s["mode"] == "bus"),
    )
    return stations_out


def interpolate_grids_and_isochrones(
    df_crossroads: pd.DataFrame,
    orig_lat: float = ORIGIN_LAT,
    orig_lon: float = ORIGIN_LON,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Interpolate 200x200 travel-time grids (IDW + light smoothing) and extract GeoJSON contours."""
    s_lat, w_lon, n_lat, e_lon = BBOX
    lats_1d = np.linspace(s_lat, n_lat, GRID_N)
    lons_1d = np.linspace(w_lon, e_lon, GRID_N)
    lon_grid, lat_grid = np.meshgrid(lons_1d, lats_1d)

    cr_xy = latlon_to_xy(
        df_crossroads["lat"].to_numpy(),
        df_crossroads["lon"].to_numpy(),
        orig_lat=orig_lat,
        orig_lon=orig_lon,
    )
    grid_xy = latlon_to_xy(
        lat_grid.ravel(),
        lon_grid.ravel(),
        orig_lat=orig_lat,
        orig_lon=orig_lon,
    )

    tree = cKDTree(cr_xy)
    k_neighbors = min(8, len(df_crossroads))
    dists, idxs = tree.query(grid_xy, k=k_neighbors)
    if k_neighbors == 1:
        dists = dists[:, None]
        idxs = idxs[:, None]

    # Metric IDW weights (power=2.5 preserves river/corridor sharpness)
    weights = 1.0 / np.maximum(dists, 12.0) ** 2.5
    weights /= weights.sum(axis=1, keepdims=True)

    # Water / park void mask: if nearest crossroad > 550m away, mark null or extrapolate slightly
    nearest_dist = dists[:, 0]
    far_penalty_min = np.maximum(0.0, (nearest_dist - 120.0) / 80.0)

    grids_payload: dict[str, Any] = {
        "bbox": list(BBOX),
        "rows": GRID_N,
        "cols": GRID_N,
        "lats": [round(float(x), 6) for x in lats_1d],
        "lons": [round(float(x), 6) for x in lons_1d],
        "modes": {},
    }
    isochrones_payload: dict[str, Any] = {}

    for mode_col, mode_key in [
        ("walk_min", "walk"),
        ("bike_min", "bike"),
        ("transit_min", "transit"),
        ("best_min", "best"),
    ]:
        vals = df_crossroads[mode_col].to_numpy(dtype=np.float64)
        interp_flat = (vals[idxs] * weights).sum(axis=1) + far_penalty_min
        grid_2d = interp_flat.reshape((GRID_N, GRID_N))
        # Mild Gaussian smoothing for clean contours without blurring across the Seine
        grid_2d = gaussian_filter(grid_2d, sigma=0.85)
        grid_2d = np.clip(grid_2d, 0.0, 120.0)

        # Store rounded compact grid
        grids_payload["modes"][mode_key] = np.round(grid_2d, 2).tolist()

        # Extract GeoJSON contour lines via contourpy
        cgen = contourpy.contour_generator(x=lons_1d, y=lats_1d, z=grid_2d)
        features: list[dict[str, Any]] = []
        for level in ISOCHRONE_LEVELS:
            lines = cgen.lines(float(level))
            for line in lines:
                if len(line) < 2:
                    continue
                coords = [[round(float(pt[0]), 5), round(float(pt[1]), 5)] for pt in line]
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "level_min": int(level),
                            "mode": mode_key,
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coords,
                        },
                    }
                )
        isochrones_payload[mode_key] = {
            "type": "FeatureCollection",
            "features": features,
        }
        logger.info(
            "Mode '%s': grid min=%.2f max=%.2f min, extracted %d contour segments",
            mode_key,
            float(grid_2d.min()),
            float(grid_2d.max()),
            len(features),
        )

    return grids_payload, isochrones_payload


def sample_crossroads_for_ui(df_crossroads: pd.DataFrame, target_count: int = 3500) -> list[dict[str, Any]]:
    """Select an even spatial subsample of crossroads for fast browser point-layer rendering."""
    if len(df_crossroads) <= target_count:
        subset = df_crossroads
    else:
        # Spatial grid binning (~60x60 cells) to keep uniform visual coverage
        s_lat, w_lon, n_lat, e_lon = BBOX
        bins = 60
        df_tmp = df_crossroads.copy()
        df_tmp["_lat_bin"] = np.clip(
            ((df_tmp["lat"] - s_lat) / (n_lat - s_lat) * bins).astype(int), 0, bins - 1
        )
        df_tmp["_lon_bin"] = np.clip(
            ((df_tmp["lon"] - w_lon) / (e_lon - w_lon) * bins).astype(int), 0, bins - 1
        )
        subset = (
            df_tmp.groupby(["_lat_bin", "_lon_bin"], group_keys=False)
            .first()
            .reset_index(drop=True)
        )
        if len(subset) > target_count:
            subset = subset.sample(n=target_count, random_state=42)

    records = [
        {
            "lat": round(float(row.lat), 6),
            "lon": round(float(row.lon), 6),
            "walk_min": round(float(row.walk_min), 1),
            "bike_min": round(float(row.bike_min), 1),
            "transit_min": round(float(row.transit_min), 1),
            "best_min": round(float(row.best_min), 1),
            "fastest_mode": str(row.fastest_mode),
        }
        for row in subset.itertuples(index=False)
    ]
    return records


def solve_seed(
    seed: dict[str, Any],
    g_walk: nx.Graph,
    g_bike: nx.Graph,
    g_transit: nx.DiGraph,
    nodes_coord: dict[int, tuple[float, float]],
    crossroad_nodes: set[int],
    valid_nodes: dict[int, tuple[float, float]],
    raw_stations: list[dict[str, Any]],
    idfm_stations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute shortest paths, crossroad travel times, grids, and isochrones for one origin seed."""
    seed_id = seed["id"]
    seed_name = seed["name"]
    orig_lat = float(seed["lat"])
    orig_lon = float(seed["lon"])

    origin_node = snap_origin_to_graph(
        orig_lat, orig_lon, seed_name, valid_nodes, nodes_coord, g_walk, g_bike
    )
    seed_crossroads = set(crossroad_nodes)
    seed_crossroads.add(origin_node)

    logger.info("[%s] Running single-source Dijkstra on walk, bike, and transit graphs...", seed_id)
    walk_sec_map = nx.single_source_dijkstra_path_length(g_walk, origin_node, weight="weight")
    bike_sec_map = nx.single_source_dijkstra_path_length(g_bike, origin_node, weight="weight")
    transit_sec_map = nx.single_source_dijkstra_path_length(g_transit, origin_node, weight="weight")

    # For pedestrian-only plazas/parks unreachable on bike, bike to nearest bikeable node and walk last meters
    bike_reachable_ids = np.fromiter(bike_sec_map.keys(), dtype=np.int64)
    bike_reachable_coords = np.array(
        [nodes_coord[nid] for nid in bike_reachable_ids], dtype=np.float64
    )
    bike_tree = cKDTree(
        latlon_to_xy(bike_reachable_coords[:, 0], bike_reachable_coords[:, 1], orig_lat, orig_lon)
    )

    rows: list[dict[str, Any]] = []
    for nid in sorted(seed_crossroads):
        w_sec = walk_sec_map.get(nid)
        t_sec = transit_sec_map.get(nid)
        b_sec = bike_sec_map.get(nid)
        if w_sec is None or t_sec is None:
            continue
        if b_sec is None:
            lat_n, lon_n = nodes_coord[nid]
            q_xy = latlon_to_xy(np.array([lat_n]), np.array([lon_n]), orig_lat, orig_lon)[0]
            dist_m, b_idx = bike_tree.query(q_xy)
            nearest_b_nid = int(bike_reachable_ids[int(b_idx)])
            b_sec = min(w_sec, bike_sec_map[nearest_b_nid] + (float(dist_m) / 1.333) + 15.0)

        # Transit can never be slower than pure walking because g_transit contains g_walk
        t_sec = min(t_sec, w_sec)

        w_min = w_sec / 60.0
        b_min = b_sec / 60.0
        t_min = t_sec / 60.0
        best_min = min(w_min, b_min, t_min)

        if w_min <= b_min and w_min <= t_min:
            fastest = "walk"
        elif b_min <= t_min:
            fastest = "bike"
        else:
            fastest = "transit"

        lat, lon = nodes_coord[nid]
        rows.append(
            {
                "node_id": int(nid),
                "lat": float(lat),
                "lon": float(lon),
                "walk_min": round(w_min, 3),
                "bike_min": round(b_min, 3),
                "transit_min": round(t_min, 3),
                "best_min": round(best_min, 3),
                "fastest_mode": fastest,
            }
        )

    df_crossroads = pd.DataFrame(rows)
    if df_crossroads.empty:
        raise RuntimeError(f"No crossroads were computed for seed {seed_id}.")

    stations_out = deduplicate_stations(raw_stations, transit_sec_map, idfm_stations)
    grids_payload, isochrones_payload = interpolate_grids_and_isochrones(
        df_crossroads, orig_lat=orig_lat, orig_lon=orig_lon
    )
    sampled_crossroads = sample_crossroads_for_ui(df_crossroads)

    meta_payload = {
        "active_seed": seed_id,
        "seeds": list(SEEDS.values()),
        "origin_address": seed["address"],
        "origin_lat": orig_lat,
        "origin_lon": orig_lon,
        "origin_node_id": int(origin_node),
        "bbox": list(BBOX),
        "total_crossroads": int(len(df_crossroads)),
        "sampled_crossroads": int(len(sampled_crossroads)),
        "total_stations": int(len(stations_out)),
        "rail_metro_tram_stations": int(sum(1 for s in stations_out if s["mode"] != "bus")),
        "bus_stops": int(sum(1 for s in stations_out if s["mode"] == "bus")),
        "isochrone_levels_min": ISOCHRONE_LEVELS,
        "providers": get_configured_providers(),
        "data_sources": [
            "OpenStreetMap (Overpass API) road, cycleway, and transit routes",
            "Île-de-France Mobilités (IDFM) station coordinates",
        ],
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return {
        "df_crossroads": df_crossroads,
        "stations_out": stations_out,
        "grids_payload": grids_payload,
        "isochrones_payload": isochrones_payload,
        "sampled_crossroads": sampled_crossroads,
        "meta_payload": meta_payload,
    }


def build_all(force_refresh: bool = False) -> None:
    """Execute full multimodal network construction and write output artifacts for all seeds."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    s_lat, w_lon, n_lat, e_lon = BBOX
    roads_query = (
        f"[out:json][timeout:180];"
        f'way["highway"~"^(primary|secondary|tertiary|residential|living_street|pedestrian|'
        f'cycleway|footway|steps|unclassified|service|trunk_link|primary_link|secondary_link|'
        f'tertiary_link)$"]({s_lat},{w_lon},{n_lat},{e_lon});'
        f"out body;>;out skel qt;"
    )
    transit_query = (
        f"[out:json][timeout:180];"
        f'relation["type"="route"]["route"~"^(subway|light_rail|train|tram|bus)$"]'
        f"({s_lat},{w_lon},{n_lat},{e_lon});"
        f"out body;node(r);out body qt;"
    )

    roads_data = query_overpass(roads_query, ROADS_CACHE, force_refresh=force_refresh)
    transit_data = query_overpass(transit_query, TRANSIT_CACHE, force_refresh=force_refresh)
    idfm_stations = fetch_idfm_stations(force_refresh=force_refresh)

    g_walk, g_bike, nodes_coord, crossroad_nodes, valid_nodes = build_street_graphs(roads_data)
    g_transit, raw_stations = build_multimodal_transit_graph(
        g_walk, nodes_coord, transit_data
    )

    # Save seeds list metadata
    seeds_list = list(SEEDS.values())
    tmp_seeds = SEEDS_OUT.with_suffix(".json.tmp")
    with tmp_seeds.open("w", encoding="utf-8") as fh:
        json.dump(seeds_list, fh, indent=2)
    tmp_seeds.replace(SEEDS_OUT)

    for seed_id, seed_cfg in SEEDS.items():
        logger.info("Computing network reachability for seed '%s' (%s)...", seed_id, seed_cfg["name"])
        res = solve_seed(
            seed=seed_cfg,
            g_walk=g_walk,
            g_bike=g_bike,
            g_transit=g_transit,
            nodes_coord=nodes_coord,
            crossroad_nodes=crossroad_nodes,
            valid_nodes=valid_nodes,
            raw_stations=raw_stations,
            idfm_stations=idfm_stations,
        )

        df_crossroads = res["df_crossroads"]
        stations_out = res["stations_out"]
        grids_payload = res["grids_payload"]
        isochrones_payload = res["isochrones_payload"]
        sampled_crossroads = res["sampled_crossroads"]
        meta_payload = res["meta_payload"]

        # Seed-specific output filenames
        seed_parquet = DATA_DIR / f"crossroads_travel_times_{seed_id}.parquet"
        seed_grids = DATA_DIR / f"heatmap_grids_{seed_id}.json"
        seed_iso = DATA_DIR / f"isochrones_{seed_id}.json"
        seed_stations = DATA_DIR / f"stations_{seed_id}.json"
        seed_sampled = DATA_DIR / f"sampled_crossroads_{seed_id}.json"
        seed_meta = DATA_DIR / f"metadata_{seed_id}.json"

        logger.info("[%s] Writing %d crossroads to %s", seed_id, len(df_crossroads), seed_parquet)
        df_crossroads.to_parquet(seed_parquet, index=False)

        artifacts = [
            (seed_grids, grids_payload),
            (seed_iso, isochrones_payload),
            (seed_stations, stations_out),
            (seed_sampled, sampled_crossroads),
            (seed_meta, meta_payload),
        ]

        # For default seed, also populate standard filenames for backward compatibility
        if seed_id == DEFAULT_SEED:
            logger.info("Writing default Parquet to %s", PARQUET_OUT)
            df_crossroads.to_parquet(PARQUET_OUT, index=False)
            artifacts.extend([
                (GRIDS_OUT, grids_payload),
                (ISOCHRONES_OUT, isochrones_payload),
                (STATIONS_OUT, stations_out),
                (SAMPLED_CROSSROADS_OUT, sampled_crossroads),
                (META_OUT, meta_payload),
            ])

        for out_path, obj in artifacts:
            tmp = out_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            tmp.replace(out_path)

    generate_overlays(roads_data=roads_data, transit_data=transit_data)
    logger.info("Pipeline completed for all seeds in %.2fs!", time.time() - t_start)


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv or "-f" in sys.argv
    build_all(force_refresh=force)

