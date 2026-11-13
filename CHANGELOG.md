# Changelog

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

