"""Verification tests for Sorbonne Université Multimodal Transport Heatmap."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from server import DATA_DIR, STATIC_DIR, app


def test_data_artifacts_exist_and_meet_scale():
    parquet_path = DATA_DIR / "crossroads_travel_times.parquet"
    assert parquet_path.exists(), "Parquet crossroad dataset missing"
    df = pd.read_parquet(parquet_path)
    assert len(df) >= 15000, f"Expected >= 15,000 crossroads, got {len(df)}"
    for col in ("node_id", "lat", "lon", "walk_min", "bike_min", "transit_min", "best_min", "fastest_mode"):
        assert col in df.columns

    # Physical consistency: transit <= walk (since walk is embedded in transit graph)
    assert (df["transit_min"] <= df["walk_min"] + 1e-6).all()
    assert (df["best_min"] <= df["walk_min"] + 1e-6).all()
    assert (df["best_min"] <= df["bike_min"] + 1e-6).all()
    assert (df["best_min"] <= df["transit_min"] + 1e-6).all()

    stations_path = DATA_DIR / "stations.json"
    assert stations_path.exists()
    stations = json.loads(stations_path.read_text(encoding="utf-8"))
    assert len(stations) >= 300, f"Expected >= 300 stations, got {len(stations)}"

    for artifact in ("heatmap_grids.json", "isochrones.json", "sampled_crossroads.json", "metadata.json"):
        p = DATA_DIR / artifact
        assert p.exists() and p.stat().st_size > 100


def test_api_endpoints_and_physical_sanity():
    with TestClient(app) as client:
        resp_root = client.get("/")
        assert resp_root.status_code == 200
        assert "Sorbonne Transport Heatmap" in resp_root.text
        assert resp_root.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp_root.headers.get("X-Frame-Options") == "DENY"
        csp = resp_root.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp

        resp_meta = client.get("/api/meta")
        assert resp_meta.status_code == 200
        meta = resp_meta.json()
        assert "7-9 quai Saint Bernard" in meta["origin_address"]
        assert meta["total_crossroads"] >= 15000
        assert resp_meta.headers.get("Cache-Control") == "no-store"

        for mode in ("walk", "bike", "transit", "best"):
            g_resp = client.get(f"/api/grid/{mode}")
            assert g_resp.status_code == 200
            grid = g_resp.json()
            assert grid["rows"] == 200 and grid["cols"] == 200
            assert len(grid["values"]) == 200

            iso_resp = client.get(f"/api/isochrones/{mode}")
            assert iso_resp.status_code == 200
            fc = iso_resp.json()
            assert fc["type"] == "FeatureCollection"
            assert len(fc["features"]) > 0

        st_resp = client.get("/api/stations?mode=rail")
        assert st_resp.status_code == 200
        assert st_resp.json()["count"] >= 200

        cr_resp = client.get("/api/crossroads")
        assert cr_resp.status_code == 200
        assert cr_resp.json()["sampled_count"] > 500

        # Origin sanity: 7-9 quai Saint Bernard
        q_origin = client.get("/api/query?lat=48.8471&lon=2.3574")
        assert q_origin.status_code == 200
        o_data = q_origin.json()
        assert o_data["walk_min"] < 1.5
        assert o_data["transit_min"] < 1.5

        # Realistic Paris landmarks from Jussieu
        landmarks = [
            ("Place Monge", 48.8430, 2.3520, 2.0, 14.0),
            ("Gare d'Austerlitz", 48.8422, 2.3649, 2.5, 16.0),
            ("Châtelet", 48.8584, 2.3470, 4.0, 20.0),
            ("Gare de Lyon", 48.8443, 2.3744, 4.0, 22.0),
            ("Odéon", 48.8522, 2.3388, 3.0, 18.0),
        ]
        for name, lat, lon, min_t, max_t in landmarks:
            q_resp = client.get(f"/api/query?lat={lat}&lon={lon}")
            assert q_resp.status_code == 200
            d = q_resp.json()
            assert min_t <= d["transit_min"] <= max_t, (
                f"{name} transit_min={d['transit_min']} outside [{min_t}, {max_t}]"
            )
            assert d["transit_min"] <= d["walk_min"] + 0.01
            assert d["optimal_exit_station"] is not None
            assert "walk_from_station_min" in d["optimal_exit_station"]
            assert d["nearest_rail_station"] is not None
            assert "walk_from_station_min" in d["nearest_rail_station"]


def test_security_input_validation_and_method_allowlist():
    with TestClient(app) as client:
        # Invalid mode
        assert client.get("/api/grid/helicopter").status_code == 400
        assert client.get("/api/isochrones/helicopter").status_code == 400
        assert client.get("/api/stations?mode=spaceship").status_code == 400

        # Out-of-bounds coordinates
        assert client.get("/api/query?lat=55.0&lon=2.35").status_code == 422
        assert client.get("/api/query?lat=48.85&lon=-10.0").status_code == 422
        assert client.get("/api/query?lat=abc&lon=2.35").status_code == 422

        # Disallowed HTTP methods
        assert client.post("/api/meta").status_code == 405
        assert client.put("/api/meta").status_code == 405
        assert client.delete("/api/meta").status_code == 405


def test_frontend_xss_and_csp_hygiene():
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    forbidden_js = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "alert(", "confirm(", "prompt("]
    for pattern in forbidden_js:
        assert pattern not in app_js, f"Forbidden DOM/JS pattern found in app.js: {pattern}"

    assert "style=" not in index_html, "Inline style attributes violate strict CSP"
    assert "onclick=" not in index_html.lower()

