# Île-de-France Multimodal Transport Heatmap (`idf-heatmap`)

Interactive web application visualizing **pedestrian**, **public transit** (Métro, RER, Transilien, Tramway, Bus), and **bicycle** travel times across **Paris and the Petite/Grande Couronne** with multi-origin seed support:

1. **Sorbonne Université — Campus Pierre et Marie Curie** (`48.8471° N, 2.3574° E`)
2. **Antony — Renaissance** (`48.7610° N, 2.2992° E`, with 10+ km southern corridor coverage including Massy, Palaiseau, Bièvres, and Orly)

---

## Methodology: Hybrid Street-Graph Dijkstra + Station Propagation

Rather than sampling a naive grid with straight-line interpolation (which blurs across rivers and misses non-monotonic transit corridors), this project computes travel times using a physical spatial graph:

1. **OpenStreetMap Street & Cycleway Graph**:
   - Ingests walkable streets, footpaths, steps, and cycling infrastructure via Overpass API within the regional bounding box `(48.665, 2.155, 48.915, 2.480)`.
   - Identifies **~400,000 crossroads** (network intersections with degree ≥ 3 or shared ways).
   - Runs single-source Dijkstra for **Pedestrian** (`4.8 km/h`, step penalties) and **Bicycle** (`18.5 km/h` on cycle tracks, `15.5 km/h` on streets, dismount on footpaths).
2. **Multimodal Public Transit Graph + Pedestrian Propagation**:
   - Ingests Métro, RER, Transilien, Tramway, and Bus routes and stations from OpenStreetMap and Île-de-France Mobilités (IDFM).
   - Models boarding/waiting times, dwell penalties, commercial speeds, and transfer costs.
   - Propagates arrival times from transit stations outward along the pedestrian street network to every crossroad.
3. **Continuous Interpolation & Isochrone Contours**:
   - Exports crossroad travel times to Parquet (`crossroads_travel_times_{seed}.parquet`).
   - Generates 200 × 200 metric inverse-distance-weighted (IDW) grids and extracts 5–60 min GeoJSON isochrones using `contourpy`.
4. **Interactive Web Interface**:
   - Self-hosted Leaflet canvas layer with dynamic color palettes (Turbo, Emerald, Magma).
   - Mode winner comparison (Transit vs. Bike vs. Walk).
   - Click-to-inspect point analysis showing snapped coordinates, distances, fastest modes, and optimal transit exit stations with last-mile walking times.

---

## Quickstart

### Prerequisites
- Python >= 3.11
- Git

### 1. Clone & Set Up Environment

```bash
git clone https://github.com/atoffano/idf-heatmap.git
cd idf-heatmap

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Precompute Network & Heatmaps

```bash
# Fetches OpenStreetMap & IDFM data, solves Dijkstra routing, and builds grids
python build_network.py

# Optional: Force fresh download from Overpass mirrors
python build_network.py --force
```

> [!NOTE]
> Precomputed artifacts are saved to `./data/` (or `/mnt/data/sorbonne-transport-map/` if present, or customize via `export DATA_DIR=/path/to/data`).

### 3. Run Tests

```bash
pytest -v
```

### 4. Start Web Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8050
```

Open [http://localhost:8050](http://localhost:8050) in your browser.

---

## Configuration (`.env`)

Copy `.env.example` to `.env` to configure optional live API calibration:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `DATA_DIR` | Custom directory path for parquet and grid artifacts (default: `./data`) |
| `IDFM_PRIM_API_KEY` | Optional Île-de-France Mobilités PRIM API token for real-time station calibration |
| `GOOGLE_MAPS_API_KEY` | Optional Google Maps Routes API token for hub transit calibration |

---

## Project Structure

```
├── build_network.py     # Graph builder, Dijkstra solver, IDW grid & isochrone pipeline
├── build_overlays.py    # Metro/RER lines and cycleway GeoJSON generator
├── server.py            # FastAPI backend (security headers, caching, spatial queries)
├── external_providers.py# Optional live calibration integrations
├── static/              # Leaflet web frontend (HTML5 canvas renderer, UI controls)
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── tests/               # Pytest suite (physical sanity, endpoints, security hygiene)
├── requirements.txt     # Pinned Python dependencies
└── pyproject.toml       # Package metadata and configuration
```
