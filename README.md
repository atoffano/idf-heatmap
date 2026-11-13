# Sorbonne Université (Jussieu) Multimodal Transport Heatmap

Interactive web application visualizing **pedestrian**, **public transit** (Métro, RER, Transilien, Tramway, Bus), and **bicycle** travel times from:

> **Sorbonne Université — Campus Pierre et Marie Curie**  
> **7-9 quai Saint Bernard, 75005 Paris** (`48.8471° N, 2.3574° E`)

across **Paris and Petite Couronne** (~10 km radius).

## Methodology: Hybrid Street-Graph Dijkstra + Station Propagation

Rather than sampling a naive grid with straight-line interpolation (which blurs across the Seine and misses non-monotonic transit corridors), this project computes travel times using a three-stage spatial pipeline:

1. **OpenStreetMap Street & Cycleway Graph**:
   - Fetches walkable streets, footpaths, steps, and cycling infrastructure in the Paris + Petite Couronne bounding box via Overpass API.
   - Identifies **all street crossroads** (intersections with degree ≥ 3 or shared across multiple OSM ways).
   - Runs exact single-source Dijkstra routing from `7-9 quai Saint Bernard` for **Pedestrian** (`4.8 km/h`, steps penalty) and **Bicycle** (`18.5 km/h` cycleways, `15.5 km/h` streets, dismount on footways/steps).
2. **Multimodal Public Transit Graph + Pedestrian Propagation**:
   - Ingests Métro, RER, Transilien, Tramway, and Bus route relations and stops from OpenStreetMap (plus IDFM station metadata, and optional Google Maps Routes API / IDFM PRIM live station overrides via `.env`).
   - Models boarding/wait penalties, dwell times, commercial speeds, and transfer/egress costs on route-specific nodes connected to the street network.
   - Single-source Dijkstra propagates station arrival times outward along the pedestrian street network to every crossroad.
3. **Continuous Interpolation & Isochrone Contours**:
   - Exports all crossroads to Parquet (`data/crossroads_travel_times.parquet`).
   - Builds `200 × 200` metric inverse-distance-weighted (IDW) + lightly smoothed grids and extracts `5, 10, 15, 20, 30, 45, 60 min` GeoJSON isochrone contours using `contourpy`.

## Project Layout

- `build_network.py` — Data ingestion, Dijkstra network solver, Parquet export, IDW grid & isochrone builder
- `external_providers.py` — Optional Google Maps Routes API / IDFM PRIM calibration helpers
- `server.py` — FastAPI backend serving APIs and static frontend on `127.0.0.1:8050`
- `static/` — Self-hosted Leaflet + HTML5 Canvas heatmap UI (`index.html`, `styles.css`, `app.js`)
- `data/` — Symlink to `/mnt/data/sorbonne-transport-map/` (Parquet, JSON grids, GeoJSON isochrones)
- `tests/` — Pytest suite verifying physical sanity, data integrity, endpoints, and security headers

## Usage

```bash
# 1. Build network & precompute travel-time grids (caches Overpass responses in data/)
.venv/bin/python build_network.py

# 2. Run verification tests
.venv/bin/pytest -v

# 3. Start web server (localhost only)
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8050
```

