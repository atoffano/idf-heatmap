# Setup & Operations Guide — Île-de-France Multimodal Transport Heatmap

Comprehensive guide to installing, configuring, precomputing spatial networks, running, and deploying `idf-heatmap`.

---

## 1. System Requirements & Prerequisites

| Resource | Minimum | Recommended | Notes |
|---|---|---|---|
| **Python** | `3.11` | `3.12` or `3.13` | Tested on Python 3.11, 3.12, 3.13 |
| **RAM** | `4 GB` | `8 GB+` | Precomputing Dijkstra across ~400k crossroads uses ~1.8 GB peak during build; serving the web app uses ~350 MB |
| **Disk Space** | `1 GB` | `2 GB` | Parquet datasets, IDW grids, GeoJSON isochrones, and raw Overpass cache |
| **OS** | Linux / macOS / Windows (WSL2) | Ubuntu 22.04 / 24.04 LTS | Linux or WSL2 recommended for fastest spatial KD-Tree performance |
| **Network** | Outbound HTTPS | Fast broadband | Required only during `build_network.py` to fetch OpenStreetMap and IDFM data |

---

## 2. Step-by-Step Installation

### Step 1: Clone the Repository

```bash
git clone https://github.com/atoffano/idf-heatmap.git
cd idf-heatmap
```

### Step 2: Create a Virtual Environment

```bash
# Using standard venv
python3 -m venv .venv
source .venv/bin/activate

# Or using conda (if preferred)
# conda create -n idf-heatmap python=3.12 -y
# conda activate idf-heatmap
```

### Step 3: Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

*(Alternatively: `pip install -e .` using `pyproject.toml`)*

### Step 4: Configure Environment (Optional)

Copy `.env.example` to create your local `.env`:

```bash
cp .env.example .env
```

Available environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `./data` (or `/mnt/data/sorbonne-transport-map` if present) | Directory path where Parquet, JSON grids, and GeoJSON files are stored |
| `IDFM_PRIM_API_KEY` | *(None)* | Optional Île-de-France Mobilités PRIM API token for real-time station calibration |
| `GOOGLE_MAPS_API_KEY` | *(None)* | Optional Google Maps Routes API key for hub station transit validation |

---

## 3. Data Precomputation Pipeline

The application visualizes travel times using a physical spatial graph (~400,000 crossroads, 6,661 transit stations). Before running the server for the first time, you must build the spatial artifacts:

```bash
# Precomputes networks and builds travel-time grids for all configured origin seeds
python build_network.py
```

### What `build_network.py` Does:
1. **Fetches OpenStreetMap Data**: Downloads all walkable and cyclable ways within the bounding box `(48.665, 2.155, 48.915, 2.480)` via Overpass API mirrors.
2. **Fetches Transit Data**: Downloads Métro, RER, Transilien, Tramway, and Bus line relations and stop nodes from OSM and Île-de-France Mobilités.
3. **Builds Graphs**:
   - `G_walk`: Pedestrian network (4.8 km/h, step penalties).
   - `G_bike`: Cyclable network (18.5 km/h on cycle tracks, 15.5 km/h on roads, dismount on stairs).
   - `G_transit`: Multimodal graph linking stations to pedestrian streets with boarding, dwell, and transfer penalties.
4. **Detects Crossroads**: Identifies ~400,000 network crossroads (intersections with degree ≥ 3 or intersecting ways).
5. **Solves Multi-Origin Dijkstra**: Computes single-source shortest paths for each seed origin:
   - **Sorbonne Université (Jussieu)**: `48.8471° N, 2.3574° E`
   - **Antony (Renaissance)**: `48.7610° N, 2.2992° E`
6. **Exports Output Artifacts** to `DATA_DIR`:
   - `crossroads_travel_times_{seed}.parquet`: Full crossroad datasets.
   - `heatmap_grids_{seed}.json`: 200×200 metric IDW interpolated grids.
   - `isochrones_{seed}.json`: 5–60 min GeoJSON contour polygons (`contourpy`).
   - `stations_{seed}.json`: Deduplicated transit stations with travel times.
   - `transit_lines.json`: Métro & RER line overlays with official line colors.
   - `bike_lanes.json`: Protected cycleway tracks and painted bike lanes.

> [!TIP]
> If you wish to force a fresh download from Overpass (bypassing local cached JSON):
> ```bash
> python build_network.py --force
> ```

---

## 4. Running the Web Application

### Development Mode (with hot-reload)

```bash
uvicorn server:app --reload --host 127.0.0.1 --port 8050
```

### Production / Server Daemon Mode

```bash
uvicorn server:app --host 0.0.0.0 --port 8050
```

Once running, access the web app at:
- **Local Machine**: [http://localhost:8050](http://localhost:8050)
- **Local Network / LAN**: `http://<server-ip>:8050`
- **Healthcheck Endpoint**: `http://<server-ip>:8050/healthz`

---

## 5. Deployment Options

### Option A: Systemd User Service (Linux)

Create `~/.config/systemd/user/idf-heatmap.service`:

```ini
[Unit]
Description=IDF Multimodal Transport Heatmap Web App
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/projects/idf-heatmap
ExecStart=%h/projects/idf-heatmap/.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8050
Restart=on-failure
RestartSec=5s
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

Enable and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now idf-heatmap.service
systemctl --user status idf-heatmap.service
```

### Option B: Reverse Proxy with Caddy

To serve with automatic HTTPS via Caddy:

```caddy
transport.yourdomain.com {
    reverse_proxy 127.0.0.1:8050 {
        header_down -X-Frame-Options
    }
}
```

### Option C: SSH Port Forwarding (Remote Server Access)

If running on a remote headless server without a public domain:

```bash
# Run on your client laptop:
ssh -L 8050:127.0.0.1:8050 user@your-server-ip
```

Then open `http://localhost:8050` in your local laptop's browser.

---

## 6. Verification & Automated Tests

Run the full pytest test suite to ensure graph integrity, physical sanity, and security:

```bash
pytest -v
```

The test suite automatically validates:
1. **Artifact Scale**: Parquet dataset exists with ≥ 15,000 crossroads and ≥ 300 transit stations.
2. **Physical Consistency**: `transit_min <= walk_min` and `best_min <= min(walk, bike, transit)`.
3. **Endpoint Security**: Strict Content Security Policy (CSP), nosniff, framing protection, coordinate bounding, and method allowlist (`GET`, `HEAD`).
4. **Multi-Origin Sanity**: Origin queries in central Paris and southern Antony/Massy corridors produce realistic travel times.
5. **Frontend Hygiene**: Absence of unsafe DOM patterns (`innerHTML`, `eval`) and strict CSP compliance.

---

## 7. Adding a Custom Seed Origin

To add a new seed location (e.g., your university campus, office, or home):

1. Open `build_network.py` and `server.py`.
2. Add an entry to the `SEEDS` dictionary:

```python
SEEDS["my_location"] = {
    "id": "my_location",
    "name": "My Campus / Office",
    "address": "123 Rue de Exemple, 75000 Paris",
    "lat": 48.8500,
    "lon": 2.3400,
    "badge": "MC",
}
```

3. If the point lies outside the existing bounding box `(48.665, 2.155, 48.915, 2.480)`, adjust `BBOX` in `build_network.py` and `build_overlays.py`.
4. Re-run:

```bash
python build_network.py
```

5. Restart the server. The new origin will automatically appear in the sidebar dropdown selector.

---

## 8. Troubleshooting

| Issue | Cause | Solution |
|---|---|---|
| `FileNotFoundError: No precomputed datasets found` | Server started before data was built | Run `python build_network.py` once to generate datasets |
| Overpass API `502` / `ReadTimeout` | Public Overpass mirror overloaded | `build_network.py` automatically retries across multiple mirrors (`ovp.dresel.systems`, `overpass.maprva.org`, `overpass-api.de`, `overpass.kumi.systems`). Re-run `python build_network.py` |
| `HTTP 405 Method Not Allowed` | Uptime check or preflight sent unhandled method | The server supports `GET` and `HEAD`. Use `curl -I http://localhost:8050/` or check `/healthz` |
| Map tiles dark / low contrast | Designed for high-contrast heatmap overlay | Tile styling is controlled by CSS class `.clean-base-tiles` in `static/styles.css` |
