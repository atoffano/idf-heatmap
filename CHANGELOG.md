# Changelog

## 2026-11-14
- Expanded Antony coverage to 10+ km perimeter in all directions:
  - Expanded OpenStreetMap and transit bounding box to `(48.665, 2.155, 48.915, 2.480)` (extending ~10.7 km south, ~10.6 km west, and ~13.2 km east of Antony).
  - Re-ingested road network (1.25M nodes, 346k ways) and re-anchored 399,662 crossroads.
  - Recomputed Dijkstra routing, IDW grids, isochrones, and 6,661 transit stations (652 rail/métro/tram, 6,009 bus stops) across both Sorbonne and Antony seeds.
  - Added Massy-Palaiseau, Parc de Sceaux, and Aéroport d'Orly preset landmark quick-inspect chips and set `minZoom: 10` for wide-area map viewing.
  - Updated `/api/query` coordinate bounds to allow queries across the full expanded southern perimeter.
- Multi-origin seed point support and Antony coverage:
  - Added new seed point origin at `8 rue de la renaissance, 92160 Antony` (`48.760967° N, 2.299217° E`) alongside `Sorbonne Université (Jussieu)`.
  - Expanded OpenStreetMap and transit bounding box south to `48.745° N` to cover Antony and the southern RER B / Tramway T10 corridor.
  - Re-ingested road network (890k nodes, 255k ways) and re-anchored 311,041 street crossroads.
  - Precomputed Dijkstra routing, 200x200 IDW grids, GeoJSON isochrones, station reachability, and sampled crossroads for both origins (`crossroads_travel_times_antony.parquet`, `heatmap_grids_antony.json`, etc.).
  - Added `/api/seeds` endpoint and enabled `?seed=` query parameter support across `/api/meta`, `/api/grid/{mode}`, `/api/isochrones/{mode}`, `/api/stations`, `/api/crossroads`, and `/api/query` (with default fallback to `sorbonne` for backwards compatibility).
  - Added interactive Seed Origin dropdown selector in the web frontend sidebar, dynamically updating the origin marker, map focus, heatmap canvas, isochrones, station times, and point inspection.
  - Added HEAD method support to `/` and `/healthz` endpoints to prevent 405 Method Not Allowed on browser preflights and uptime health checks.
  - Increased sliding-window rate limit to 600 req/min and added X-Forwarded-For support for reverse proxies.
  - Restarted live FastAPI / Uvicorn server daemon on `http://0.0.0.0:8050`.

## 2026-11-13
- Initial release of `sorbonne-transport-map`:
  - Hybrid street-graph Dijkstra routing + multimodal transit station propagation from Sorbonne Université Campus Pierre et Marie Curie (`7-9 quai Saint Bernard, 75005 Paris`).
  - OpenStreetMap (Overpass API) road/cycleway/transit route ingestion plus optional Google Maps Routes API / IDFM PRIM calibration support in `external_providers.py`.
  - Parquet crossroad export (`/mnt/data/sorbonne-transport-map/crossroads_travel_times.parquet`), 200x200 IDW interpolated grids, and `contourpy` GeoJSON isochrones (5–60 min).
  - Hardened FastAPI server (`server.py`) with strict CSP, security headers, rate limiting, and coordinate validation.
  - Interactive Leaflet Canvas heatmap web UI with mode toggles (Public Transit, Pedestrian, Bicycle, Best Mode, Mode Winner), layer toggles (Heatmap, Isochrones, Stations, Sampled Crossroads), max-time cutoff slider, and click-to-inspect modal breakdown.
  - Added explicit `walk_from_station_min` and `optimal_exit_station` last-mile station walk breakdown in `/api/query` and Point Inspector UI.
  - Switched basemap tiles to OpenStreetMap with low-contrast dark styling and updated CSP headers.
  - Added mode-linked network overlays (`build_overlays.py`, `/api/transit-lines`, `/api/bike-lanes`): official-color Métro (1–14) + RER (A–E) lines on Public Services / Best / Mode Winner views, and protected cycleways + painted bike lanes on Bicycle view.

