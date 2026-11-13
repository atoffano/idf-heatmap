"""Extract Métro/RER line geometries and bicycle lane geometries into GeoJSON overlays."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("/mnt/data/sorbonne-transport-map")
ROADS_CACHE = DATA_DIR / "raw_osm_roads.json"
TRANSIT_CACHE = DATA_DIR / "raw_osm_transit.json"
TRANSIT_LINES_OUT = DATA_DIR / "transit_lines.json"
BIKE_LANES_OUT = DATA_DIR / "bike_lanes.json"

BBOX = (48.775, 2.240, 48.915, 2.470)

METRO_REFS = frozenset(
    {"1", "2", "3", "3bis", "4", "5", "6", "7", "7bis", "8", "9", "10", "11", "12", "13", "14"}
)
RER_REFS = frozenset({"A", "B", "C", "D", "E"})

DEFAULT_LINE_COLORS = {
    ("Métro", "1"): "#ffbe00",
    ("Métro", "2"): "#0055c8",
    ("Métro", "3"): "#6e6e00",
    ("Métro", "3bis"): "#82c8e6",
    ("Métro", "4"): "#a0006e",
    ("Métro", "5"): "#ff7f32",
    ("Métro", "6"): "#82dc73",
    ("Métro", "7"): "#ff82b4",
    ("Métro", "7bis"): "#82dc73",
    ("Métro", "8"): "#d282be",
    ("Métro", "9"): "#b6bd00",
    ("Métro", "10"): "#dc9600",
    ("Métro", "11"): "#6e491e",
    ("Métro", "12"): "#00643c",
    ("Métro", "13"): "#82c8e6",
    ("Métro", "14"): "#640082",
    ("RER", "A"): "#e3051c",
    ("RER", "B"): "#5291ce",
    ("RER", "C"): "#ffcd2c",
    ("RER", "D"): "#00824e",
    ("RER", "E"): "#c04191",
}

TRACK_VALUES = frozenset({"track", "opposite_track"})
LANE_VALUES = frozenset({"lane", "opposite_lane", "share_busway"})
CYCLE_TAG_KEYS = ("cycleway", "cycleway:both", "cycleway:right", "cycleway:left")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def _extract_ordered_stops(
    rel: dict[str, Any], transit_nodes: dict[int, dict[str, Any]]
) -> list[int]:
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
    deduped: list[int] = []
    for nid in chosen:
        if not deduped or deduped[-1] != nid:
            deduped.append(nid)
    return deduped


def _station_key(node_el: dict[str, Any]) -> str:
    tags = node_el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if name:
        return f"name:{name.lower()}"
    return f"id:{node_el['id']}"


def build_transit_lines_geojson(transit_data: dict[str, Any]) -> dict[str, Any]:
    """Build GeoJSON FeatureCollection for Paris Métro (1-14) and RER (A-E) lines."""
    t_nodes = {
        int(el["id"]): el
        for el in transit_data.get("elements", [])
        if el.get("type") == "node" and el.get("lat") is not None and el.get("lon") is not None
    }
    t_rels = [
        el for el in transit_data.get("elements", []) if el.get("type") == "relation"
    ]

    grouped_rels: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    line_colors: dict[tuple[str, str], str] = {}

    for rel in t_rels:
        tags = rel.get("tags") or {}
        rtype = tags.get("route", "")
        ref = (tags.get("ref") or "").strip()
        if rtype == "subway" and ref in METRO_REFS:
            key = ("Métro", ref)
        elif rtype in {"train", "light_rail"} and ref in RER_REFS:
            key = ("RER", ref)
        else:
            continue
        grouped_rels[key].append(rel)
        if key not in line_colors and tags.get("colour"):
            line_colors[key] = str(tags["colour"]).strip()

    s_lat, w_lon, n_lat, e_lon = BBOX
    pad = 0.02

    def in_padded_bbox(lat: float, lon: float) -> bool:
        return (s_lat - pad <= lat <= n_lat + pad) and (w_lon - pad <= lon <= e_lon + pad)

    features: list[dict[str, Any]] = []
    for key in sorted(grouped_rels.keys(), key=lambda x: (0 if x[0] == "RER" else 1, x[1])):
        network, ref = key
        rels = grouped_rels[key]

        # Pass 1: collapse opposite-direction stop platforms per station name
        station_coords_acc: dict[str, list[tuple[float, float]]] = defaultdict(list)
        rel_stops: list[list[int]] = []
        for rel in rels:
            stops = _extract_ordered_stops(rel, t_nodes)
            if len(stops) < 2:
                continue
            rel_stops.append(stops)
            for sid in stops:
                el = t_nodes[sid]
                station_coords_acc[_station_key(el)].append(
                    (float(el["lat"]), float(el["lon"]))
                )

        station_center: dict[str, tuple[float, float]] = {}
        for skey, pts in station_coords_acc.items():
            mean_lat = sum(p[0] for p in pts) / len(pts)
            mean_lon = sum(p[1] for p in pts) / len(pts)
            station_center[skey] = (mean_lat, mean_lon)

        # Pass 2: build deduplicated undirected station-to-station segments
        seen_segments: set[tuple[tuple[float, float], tuple[float, float]]] = set()
        line_segments: list[list[list[float]]] = []

        for stops in rel_stops:
            for i in range(len(stops) - 1):
                sk1 = _station_key(t_nodes[stops[i]])
                sk2 = _station_key(t_nodes[stops[i + 1]])
                if sk1 == sk2:
                    continue
                lat1, lon1 = station_center[sk1]
                lat2, lon2 = station_center[sk2]
                if not (in_padded_bbox(lat1, lon1) or in_padded_bbox(lat2, lon2)):
                    continue
                if _haversine_m(lat1, lon1, lat2, lon2) > 6000.0:
                    continue

                p1 = (round(lon1, 5), round(lat1, 5))
                p2 = (round(lon2, 5), round(lat2, 5))
                if p1 == p2:
                    continue
                seg_id = (p1, p2) if p1 < p2 else (p2, p1)
                if seg_id in seen_segments:
                    continue
                seen_segments.add(seg_id)
                line_segments.append([[p1[0], p1[1]], [p2[0], p2[1]]])

        if not line_segments:
            continue

        color = DEFAULT_LINE_COLORS.get(key) or line_colors.get(key) or "#38bdf8"
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "network": network,
                    "ref": ref,
                    "label": f"{network} {ref}",
                    "color": color,
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": line_segments,
                },
            }
        )

    logger.info("Extracted %d Métro & RER line features", len(features))
    return {"type": "FeatureCollection", "features": features}


def build_bike_lanes_geojson(roads_data: dict[str, Any]) -> dict[str, Any]:
    """Build GeoJSON FeatureCollection for protected cycleways and painted bike lanes."""
    nodes_coord: dict[int, tuple[float, float]] = {}
    ways: list[dict[str, Any]] = []

    for el in roads_data.get("elements", []):
        etype = el.get("type")
        if etype == "node" and el.get("lat") is not None and el.get("lon") is not None:
            nodes_coord[int(el["id"])] = (float(el["lat"]), float(el["lon"]))
        elif etype == "way" and el.get("nodes"):
            ways.append(el)

    s_lat, w_lon, n_lat, e_lon = BBOX
    track_lines: list[list[list[float]]] = []
    lane_lines: list[list[list[float]]] = []

    for way in ways:
        tags = way.get("tags") or {}
        highway = tags.get("highway", "")
        cycle_vals = {tags.get(k, "") for k in CYCLE_TAG_KEYS}

        if highway == "cycleway" or (cycle_vals & TRACK_VALUES):
            tier = "track"
        elif cycle_vals & LANE_VALUES:
            tier = "lane"
        else:
            continue

        coords: list[list[float]] = []
        any_in_bbox = False
        for nid_raw in way["nodes"]:
            pt = nodes_coord.get(int(nid_raw))
            if pt is None:
                continue
            lat, lon = pt
            if s_lat <= lat <= n_lat and w_lon <= lon <= e_lon:
                any_in_bbox = True
            coords.append([round(lon, 5), round(lat, 5)])

        if not any_in_bbox or len(coords) < 2:
            continue

        if tier == "track":
            track_lines.append(coords)
        else:
            lane_lines.append(coords)

    logger.info(
        "Extracted bike infrastructure: %d protected cycleway tracks, %d painted bike lanes",
        len(track_lines),
        len(lane_lines),
    )

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "tier": "track",
                    "label": "Protected Cycleway (Piste cyclable)",
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": track_lines,
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "tier": "lane",
                    "label": "Bike Lane (Bande cyclable)",
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": lane_lines,
                },
            },
        ],
    }


def generate_overlays(
    roads_data: dict[str, Any] | None = None,
    transit_data: dict[str, Any] | None = None,
) -> None:
    """Generate transit_lines.json and bike_lanes.json in DATA_DIR."""
    if transit_data is None:
        if not TRANSIT_CACHE.exists():
            raise FileNotFoundError(f"Missing {TRANSIT_CACHE}")
        with TRANSIT_CACHE.open("r", encoding="utf-8") as fh:
            transit_data = json.load(fh)

    if roads_data is None:
        if not ROADS_CACHE.exists():
            raise FileNotFoundError(f"Missing {ROADS_CACHE}")
        with ROADS_CACHE.open("r", encoding="utf-8") as fh:
            roads_data = json.load(fh)

    transit_fc = build_transit_lines_geojson(transit_data)
    bike_fc = build_bike_lanes_geojson(roads_data)

    for out_path, payload in [
        (TRANSIT_LINES_OUT, transit_fc),
        (BIKE_LANES_OUT, bike_fc),
    ]:
        tmp = out_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(out_path)
        logger.info("Saved %s (%.1f KB)", out_path, out_path.stat().st_size / 1024.0)


if __name__ == "__main__":
    generate_overlays()
