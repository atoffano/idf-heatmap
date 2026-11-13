(() => {
  "use strict";

  const ORIGIN_COORDS = [48.8471, 2.3574];
  const LANDMARKS = [
    { name: "Châtelet", lat: 48.8584, lon: 2.347 },
    { name: "Gare de Lyon", lat: 48.8443, lon: 2.3744 },
    { name: "Gare du Nord", lat: 48.8809, lon: 2.3553 },
    { name: "Montparnasse", lat: 48.8421, lon: 2.3219 },
    { name: "Nation", lat: 48.8483, lon: 2.3959 },
    { name: "La Défense", lat: 48.8886, lon: 2.2442 },
  ];

  const PALETTES = {
    turbo: [
      [0.0, [20, 184, 166]],
      [0.2, [56, 189, 248]],
      [0.45, [250, 204, 21]],
      [0.72, [249, 115, 22]],
      [1.0, [225, 29, 72]],
    ],
    emerald: [
      [0.0, [16, 185, 129]],
      [0.33, [52, 211, 153]],
      [0.65, [251, 191, 36]],
      [1.0, [139, 92, 246]],
    ],
    magma: [
      [0.0, [252, 253, 191]],
      [0.3, [252, 137, 97]],
      [0.65, [182, 54, 121]],
      [1.0, [44, 17, 95]],
    ],
  };

  const ISO_COLORS = {
    5: "#2dd4bf",
    10: "#38bdf8",
    15: "#60a5fa",
    20: "#facc15",
    30: "#fb923c",
    45: "#f43f5e",
    60: "#c084fc",
  };

  const MODE_LABELS = {
    transit: "Public Services",
    walk: "Pedestrian",
    bike: "Bicycle",
    best: "Fastest Overall",
    winner: "Mode Winner",
  };

  const appState = {
    activeMode: "transit",
    palette: "turbo",
    cutoffMin: 60,
    opacity: 0.62,
    showHeatmap: true,
    showIsochrones: true,
    showStations: true,
    showBusStops: false,
    showCrossroads: false,
    meta: null,
    grids: {},
    isochrones: {},
    stations: null,
    crossroads: null,
    map: null,
    heatmapLayer: null,
    isoLayer: null,
    stationLayer: null,
    busLayer: null,
    crossroadLayer: null,
    queryMarker: null,
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function interpolateColor(tNorm, stops) {
    const t = Math.max(0, Math.min(1, tNorm));
    for (let i = 0; i < stops.length - 1; i += 1) {
      const [t0, c0] = stops[i];
      const [t1, c1] = stops[i + 1];
      if (t >= t0 && t <= t1) {
        const f = (t - t0) / Math.max(1e-6, t1 - t0);
        return [
          Math.round(c0[0] + (c1[0] - c0[0]) * f),
          Math.round(c0[1] + (c1[1] - c0[1]) * f),
          Math.round(c0[2] + (c1[2] - c0[2]) * f),
        ];
      }
    }
    return stops[stops.length - 1][1];
  }

  function buildPopupNode(title, lines) {
    const box = el("div", "popup-box");
    box.appendChild(el("div", "popup-title", title));
    for (const line of lines) {
      box.appendChild(el("div", "popup-meta", line));
    }
    return box;
  }

  async function fetchJson(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status} for ${url}`);
    }
    return resp.json();
  }

  async function ensureGrid(mode) {
    if (appState.grids[mode]) {
      return appState.grids[mode];
    }
    const data = await fetchJson(`/api/grid/${encodeURIComponent(mode)}`);
    appState.grids[mode] = data;
    return data;
  }

  async function ensureIsochrones(mode) {
    const targetMode = mode === "winner" ? "best" : mode;
    if (appState.isochrones[targetMode]) {
      return appState.isochrones[targetMode];
    }
    const data = await fetchJson(`/api/isochrones/${encodeURIComponent(targetMode)}`);
    appState.isochrones[targetMode] = data;
    return data;
  }

  const HeatmapCanvasLayer = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      this._canvas = L.DomUtil.create("canvas", "leaflet-layer");
      this._canvas.style.pointerEvents = "none";
      this._offscreen = document.createElement("canvas");
      map.getPanes().overlayPane.appendChild(this._canvas);
      map.on("moveend zoomend viewreset resize", this._draw, this);
      this._draw();
    },

    onRemove(map) {
      map.off("moveend zoomend viewreset resize", this._draw, this);
      if (this._canvas && this._canvas.parentNode) {
        this._canvas.parentNode.removeChild(this._canvas);
      }
    },

    redraw() {
      this._draw();
    },

    _draw() {
      if (!this._map || !this._canvas) {
        return;
      }
      if (!appState.showHeatmap) {
        this._canvas.width = 0;
        this._canvas.height = 0;
        return;
      }

      const mode = appState.activeMode;
      const baseGrid =
        mode === "winner" ? appState.grids.transit : appState.grids[mode];
      if (!baseGrid) {
        return;
      }

      const walkGrid = appState.grids.walk;
      const bikeGrid = appState.grids.bike;
      const transitGrid = appState.grids.transit;
      if (mode === "winner" && (!walkGrid || !bikeGrid || !transitGrid)) {
        return;
      }

      const [sLat, wLon, nLat, eLon] = baseGrid.bbox;
      const rows = baseGrid.rows;
      const cols = baseGrid.cols;

      const topLeft = this._map.latLngToLayerPoint([nLat, wLon]);
      const bottomRight = this._map.latLngToLayerPoint([sLat, eLon]);
      const width = Math.max(1, Math.round(bottomRight.x - topLeft.x));
      const height = Math.max(1, Math.round(bottomRight.y - topLeft.y));

      L.DomUtil.setPosition(this._canvas, topLeft);
      this._canvas.width = width;
      this._canvas.height = height;

      this._offscreen.width = cols;
      this._offscreen.height = rows;
      const octx = this._offscreen.getContext("2d");
      const img = octx.createImageData(cols, rows);
      const data = img.data;

      const paletteStops = PALETTES[appState.palette] || PALETTES.turbo;
      const cutoff = appState.cutoffMin;
      const alphaByte = Math.round(appState.opacity * 255);

      for (let r = 0; r < rows; r += 1) {
        // Canvas row 0 is North (top), grid row 0 is South (bottom)
        const srcRow = rows - 1 - r;
        for (let c = 0; c < cols; c += 1) {
          const pxIdx = (r * cols + c) * 4;

          if (mode === "winner") {
            const wVal = walkGrid.values[srcRow][c];
            const bVal = bikeGrid.values[srcRow][c];
            const tVal = transitGrid.values[srcRow][c];
            const minVal = Math.min(wVal, bVal, tVal);
            if (minVal > cutoff) {
              data[pxIdx + 3] = 0;
              continue;
            }
            let rgb;
            if (wVal <= bVal && wVal <= tVal) {
              rgb = [16, 185, 129]; // Walk emerald
            } else if (bVal <= tVal) {
              rgb = [245, 158, 11]; // Bike amber
            } else {
              rgb = [56, 189, 248]; // Transit sky blue
            }
            data[pxIdx] = rgb[0];
            data[pxIdx + 1] = rgb[1];
            data[pxIdx + 2] = rgb[2];
            data[pxIdx + 3] = alphaByte;
          } else {
            const val = baseGrid.values[srcRow][c];
            if (val > cutoff) {
              data[pxIdx + 3] = 0;
              continue;
            }
            const tNorm = val / Math.max(5, cutoff);
            const [red, green, blue] = interpolateColor(tNorm, paletteStops);
            data[pxIdx] = red;
            data[pxIdx + 1] = green;
            data[pxIdx + 2] = blue;
            data[pxIdx + 3] = alphaByte;
          }
        }
      }

      octx.putImageData(img, 0, 0);
      const ctx = this._canvas.getContext("2d");
      ctx.clearRect(0, 0, width, height);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(this._offscreen, 0, 0, width, height);
    },
  });

  function sampleGridAtLatLng(grid, lat, lon) {
    if (!grid) {
      return null;
    }
    const [sLat, wLon, nLat, eLon] = grid.bbox;
    if (lat < sLat || lat > nLat || lon < wLon || lon > eLon) {
      return null;
    }
    const rFloat = ((lat - sLat) / (nLat - sLat)) * (grid.rows - 1);
    const cFloat = ((lon - wLon) / (eLon - wLon)) * (grid.cols - 1);
    const r0 = Math.max(0, Math.min(grid.rows - 2, Math.floor(rFloat)));
    const c0 = Math.max(0, Math.min(grid.cols - 2, Math.floor(cFloat)));
    const dr = rFloat - r0;
    const dc = cFloat - c0;

    const v00 = grid.values[r0][c0];
    const v01 = grid.values[r0][c0 + 1];
    const v10 = grid.values[r0 + 1][c0];
    const v11 = grid.values[r0 + 1][c0 + 1];

    const v0 = v00 + (v01 - v00) * dc;
    const v1 = v10 + (v11 - v10) * dc;
    return v0 + (v1 - v0) * dr;
  }

  function updateLegend() {
    const legendTitle = document.getElementById("legend-title");
    const scaleBox = document.getElementById("legend-scale");
    const catBox = document.getElementById("legend-categorical");
    const legendBar = document.getElementById("legend-bar");
    const ticksBox = document.getElementById("legend-ticks");

    if (appState.activeMode === "winner") {
      legendTitle.textContent = "Fastest Mode by Zone";
      scaleBox.classList.add("hidden");
      catBox.classList.remove("hidden");
      return;
    }

    legendTitle.textContent = `${MODE_LABELS[appState.activeMode]} Time from Jussieu`;
    scaleBox.classList.remove("hidden");
    catBox.classList.add("hidden");

    const stops = PALETTES[appState.palette] || PALETTES.turbo;
    const cssStops = stops
      .map(([pos, rgb]) => `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]}) ${Math.round(pos * 100)}%`)
      .join(", ");
    legendBar.style.background = `linear-gradient(90deg, ${cssStops})`;

    const cutoff = appState.cutoffMin;
    const tickValues = [0, Math.round(cutoff * 0.25), Math.round(cutoff * 0.5), Math.round(cutoff * 0.75), cutoff];
    const tickNodes = tickValues.map((val) => el("span", null, `${val}m`));
    ticksBox.replaceChildren(...tickNodes);
  }

  async function renderIsochrones() {
    if (appState.isoLayer) {
      appState.map.removeLayer(appState.isoLayer);
      appState.isoLayer = null;
    }
    if (!appState.showIsochrones) {
      return;
    }
    const fc = await ensureIsochrones(appState.activeMode);
    const filtered = {
      type: "FeatureCollection",
      features: fc.features.filter(
        (f) => f.properties.level_min <= appState.cutoffMin
      ),
    };

    appState.isoLayer = L.geoJSON(filtered, {
      style(feature) {
        const lvl = feature.properties.level_min;
        return {
          color: ISO_COLORS[lvl] || "#ffffff",
          weight: lvl === 15 || lvl === 30 || lvl === 45 ? 2.1 : 1.3,
          opacity: 0.85,
          dashArray: lvl >= 45 ? "4 4" : null,
        };
      },
      onEachFeature(feature, layer) {
        const lvl = feature.properties.level_min;
        layer.bindTooltip(`${lvl} min isochrone`, { sticky: true });
      },
    });
    appState.isoLayer.addTo(appState.map);
  }

  async function renderStations() {
    if (!appState.stations) {
      const resp = await fetchJson("/api/stations?mode=all");
      appState.stations = resp.stations || [];
    }

    if (appState.stationLayer) {
      appState.map.removeLayer(appState.stationLayer);
      appState.stationLayer = null;
    }
    if (appState.busLayer) {
      appState.map.removeLayer(appState.busLayer);
      appState.busLayer = null;
    }

    if (appState.showStations) {
      const railMarkers = [];
      for (const st of appState.stations) {
        if (st.mode === "bus") {
          continue;
        }
        const color =
          st.mode === "train" || st.mode === "light_rail"
            ? "#f43f5e"
            : st.mode === "tram"
            ? "#a78bfa"
            : "#38bdf8";
        const marker = L.circleMarker([st.lat, st.lon], {
          radius: st.mode === "subway" ? 4.2 : 5.2,
          color: "#0f172a",
          weight: 1.2,
          fillColor: color,
          fillOpacity: 0.92,
        });
        marker.bindPopup(() => {
          const linesTxt = st.lines && st.lines.length ? `Lines: ${st.lines.join(", ")}` : "";
          return buildPopupNode(st.name, [
            `${st.mode_label} · ${st.transit_min.toFixed(1)} min from Jussieu`,
            linesTxt,
          ]);
        });
        railMarkers.push(marker);
      }
      appState.stationLayer = L.layerGroup(railMarkers).addTo(appState.map);
    }

    if (appState.showBusStops) {
      const busMarkers = [];
      for (const st of appState.stations) {
        if (st.mode !== "bus") {
          continue;
        }
        const marker = L.circleMarker([st.lat, st.lon], {
          radius: 2.5,
          color: "#0f172a",
          weight: 0.6,
          fillColor: "#94a3b8",
          fillOpacity: 0.75,
        });
        marker.bindPopup(() =>
          buildPopupNode(st.name, [
            `Bus stop · ${st.transit_min.toFixed(1)} min from Jussieu`,
            st.lines && st.lines.length ? `Lines: ${st.lines.join(", ")}` : "",
          ])
        );
        busMarkers.push(marker);
      }
      appState.busLayer = L.layerGroup(busMarkers).addTo(appState.map);
    }
  }

  async function renderCrossroads() {
    if (appState.crossroadLayer) {
      appState.map.removeLayer(appState.crossroadLayer);
      appState.crossroadLayer = null;
    }
    if (!appState.showCrossroads) {
      return;
    }
    if (!appState.crossroads) {
      const data = await fetchJson("/api/crossroads");
      appState.crossroads = data.crossroads || [];
    }

    const markers = [];
    for (const cr of appState.crossroads) {
      const dot = L.circleMarker([cr.lat, cr.lon], {
        radius: 2.2,
        stroke: false,
        fillColor: "#fde047",
        fillOpacity: 0.7,
      });
      dot.bindPopup(() =>
        buildPopupNode("OSM Street Crossroad", [
          `Transit: ${cr.transit_min} min | Bike: ${cr.bike_min} min | Walk: ${cr.walk_min} min`,
          `Fastest: ${MODE_LABELS[cr.fastest_mode] || cr.fastest_mode}`,
        ])
      );
      markers.push(dot);
    }
    appState.crossroadLayer = L.layerGroup(markers).addTo(appState.map);
  }

  function renderInspector(data, customTitle) {
    const promptEl = document.getElementById("inspector-prompt");
    const resultsEl = document.getElementById("inspector-results");
    const titleEl = document.getElementById("insp-title");
    const coordsEl = document.getElementById("insp-coords");
    const badgeEl = document.getElementById("insp-winner-badge");
    const barsEl = document.getElementById("insp-bars");
    const stationBox = document.getElementById("insp-station-box");

    promptEl.classList.add("hidden");
    resultsEl.classList.remove("hidden");

    titleEl.textContent = customTitle || `Crossroad (${data.distance_from_origin_km} km straight-line)`;
    coordsEl.textContent = `${data.snapped_lat.toFixed(5)}, ${data.snapped_lon.toFixed(5)}`;

    const winner = data.fastest_mode;
    badgeEl.className = `winner-badge winner-badge-${winner}`;
    badgeEl.textContent = `${MODE_LABELS[winner] || winner} fastest`;

    const maxBar = Math.max(data.walk_min, data.bike_min, data.transit_min, 15);
    const items = [
      { key: "transit", label: "Public Services (incl. station walk)", val: data.transit_min },
      { key: "bike", label: "Bicycle", val: data.bike_min },
      { key: "walk", label: "Pedestrian", val: data.walk_min },
    ];

    const barNodes = items.map((item) => {
      const wrap = el("div", "bar-item");
      const head = el("div", "bar-item-head");
      head.appendChild(el("span", null, item.label));
      head.appendChild(el("strong", "mono-val", `${item.val.toFixed(1)} min`));
      const track = el("div", "bar-track");
      const fill = el("div", `bar-fill bar-fill-${item.key}`);
      const pct = Math.max(4, Math.min(100, (item.val / maxBar) * 100));
      fill.style.width = `${pct.toFixed(1)}%`;
      track.appendChild(fill);
      wrap.appendChild(head);
      wrap.appendChild(track);
      return wrap;
    });
    barsEl.replaceChildren(...barNodes);

    const stationLines = [];
    if (data.optimal_exit_station) {
      const opt = data.optimal_exit_station;
      stationLines.push(
        el(
          "div",
          null,
          `Via ${opt.name} (${opt.mode_label}): ${opt.transit_min.toFixed(1)} min to station + ${opt.walk_from_station_min.toFixed(1)} min walk (${opt.distance_m} m)`
        )
      );
    }
    if (data.nearest_rail_station) {
      const rs = data.nearest_rail_station;
      stationLines.push(
        el(
          "div",
          null,
          `Nearest Rail/Métro: ${rs.name} (${rs.mode_label}, ${rs.distance_m} m · +${rs.walk_from_station_min.toFixed(1)} min walk)`
        )
      );
    } else if (data.nearest_station) {
      const ns = data.nearest_station;
      stationLines.push(
        el(
          "div",
          null,
          `Nearest Stop: ${ns.name} (${ns.mode_label}, ${ns.distance_m} m · +${ns.walk_from_station_min.toFixed(1)} min walk)`
        )
      );
    }
    stationBox.replaceChildren(...stationLines);
  }

  async function inspectLocation(lat, lon, customTitle) {
    try {
      const data = await fetchJson(
        `/api/query?lat=${encodeURIComponent(lat.toFixed(6))}&lon=${encodeURIComponent(lon.toFixed(6))}`
      );
      if (appState.queryMarker) {
        appState.map.removeLayer(appState.queryMarker);
      }
      appState.queryMarker = L.circleMarker([data.snapped_lat, data.snapped_lon], {
        radius: 7,
        color: "#ffffff",
        weight: 2.5,
        fillColor: "#f43f5e",
        fillOpacity: 0.95,
      }).addTo(appState.map);

      renderInspector(data, customTitle);
    } catch (_err) {
      const promptEl = document.getElementById("inspector-prompt");
      promptEl.classList.remove("hidden");
      promptEl.textContent = "Point outside Paris + Petite Couronne coverage area.";
    }
  }

  async function switchMode(newMode) {
    appState.activeMode = newMode;
    document.querySelectorAll(".mode-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === newMode);
    });

    if (newMode === "winner") {
      await Promise.all([
        ensureGrid("walk"),
        ensureGrid("bike"),
        ensureGrid("transit"),
      ]);
    } else {
      await ensureGrid(newMode);
    }

    updateLegend();
    appState.heatmapLayer.redraw();
    await renderIsochrones();
  }

  function setupEvents() {
    document.getElementById("mode-buttons").addEventListener("click", (evt) => {
      const btn = evt.target.closest(".mode-btn");
      if (!btn || !btn.dataset.mode) {
        return;
      }
      switchMode(btn.dataset.mode);
    });

    document.getElementById("toggle-heatmap").addEventListener("change", (evt) => {
      appState.showHeatmap = evt.target.checked;
      appState.heatmapLayer.redraw();
    });

    document.getElementById("toggle-isochrones").addEventListener("change", (evt) => {
      appState.showIsochrones = evt.target.checked;
      renderIsochrones();
    });

    document.getElementById("toggle-stations").addEventListener("change", (evt) => {
      appState.showStations = evt.target.checked;
      renderStations();
    });

    document.getElementById("toggle-bus-stops").addEventListener("change", (evt) => {
      appState.showBusStops = evt.target.checked;
      renderStations();
    });

    document.getElementById("toggle-crossroads").addEventListener("change", (evt) => {
      appState.showCrossroads = evt.target.checked;
      renderCrossroads();
    });

    document.getElementById("slider-cutoff").addEventListener("input", (evt) => {
      appState.cutoffMin = Number(evt.target.value);
      document.getElementById("label-cutoff").textContent = `${appState.cutoffMin} min`;
      updateLegend();
      appState.heatmapLayer.redraw();
      renderIsochrones();
    });

    document.getElementById("slider-opacity").addEventListener("input", (evt) => {
      const pct = Number(evt.target.value);
      appState.opacity = pct / 100;
      document.getElementById("label-opacity").textContent = `${pct}%`;
      appState.heatmapLayer.redraw();
    });

    document.getElementById("select-palette").addEventListener("change", (evt) => {
      appState.palette = evt.target.value;
      updateLegend();
      appState.heatmapLayer.redraw();
    });

    document.getElementById("btn-center-origin").addEventListener("click", () => {
      appState.map.setView(ORIGIN_COORDS, 13, { animate: true });
    });

    const modal = document.getElementById("methodology-modal");
    document.getElementById("btn-methodology").addEventListener("click", () => {
      modal.classList.remove("hidden");
    });
    document.getElementById("btn-close-modal").addEventListener("click", () => {
      modal.classList.add("hidden");
    });
    modal.addEventListener("click", (evt) => {
      if (evt.target === modal) {
        modal.classList.add("hidden");
      }
    });

    const presetsBox = document.getElementById("landmark-presets");
    const chips = LANDMARKS.map((lm) => {
      const chip = el("button", "preset-chip", lm.name);
      chip.type = "button";
      chip.addEventListener("click", () => {
        appState.map.panTo([lm.lat, lm.lon], { animate: true });
        inspectLocation(lm.lat, lm.lon, lm.name);
      });
      return chip;
    });
    presetsBox.replaceChildren(...chips);
  }

  function initMap() {
    const map = L.map("map", {
      center: ORIGIN_COORDS,
      zoom: 13,
      minZoom: 11,
      maxZoom: 17,
      zoomControl: true,
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      subdomains: "abc",
      maxZoom: 19,
      attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\" target=\"_blank\" rel=\"noopener\">OpenStreetMap</a> contributors",
      className: "clean-base-tiles",
    }).addTo(map);

    appState.map = map;
    appState.heatmapLayer = new HeatmapCanvasLayer().addTo(map);

    // Sorbonne Université origin marker (halo + core)
    L.circleMarker(ORIGIN_COORDS, {
      radius: 14,
      stroke: false,
      fillColor: "#3b82f6",
      fillOpacity: 0.28,
    }).addTo(map);

    const originPin = L.circleMarker(ORIGIN_COORDS, {
      radius: 7,
      color: "#ffffff",
      weight: 2.5,
      fillColor: "#2563eb",
      fillOpacity: 1.0,
    }).addTo(map);

    originPin.bindPopup(() =>
      buildPopupNode("Sorbonne Université — Campus Pierre et Marie Curie", [
        "7-9 quai Saint Bernard, 75005 Paris",
        "Origin (0.0 min)",
      ])
    );

    const hoverHud = document.getElementById("hover-hud");
    const hoverText = document.getElementById("hover-hud-text");

    map.on("mousemove", (evt) => {
      const { lat, lng } = evt.latlng;
      const tVal = sampleGridAtLatLng(appState.grids.transit, lat, lng);
      const wVal = sampleGridAtLatLng(appState.grids.walk, lat, lng);
      const bVal = sampleGridAtLatLng(appState.grids.bike, lat, lng);
      if (tVal === null && wVal === null && bVal === null) {
        hoverHud.classList.add("hidden");
        return;
      }
      const parts = [];
      if (tVal !== null) parts.push(`Transit: ${tVal.toFixed(1)}m`);
      if (bVal !== null) parts.push(`Bike: ${bVal.toFixed(1)}m`);
      if (wVal !== null) parts.push(`Walk: ${wVal.toFixed(1)}m`);
      hoverText.textContent = parts.join(" · ");
      hoverHud.classList.remove("hidden");
    });

    map.on("mouseout", () => {
      hoverHud.classList.add("hidden");
    });

    map.on("click", (evt) => {
      inspectLocation(evt.latlng.lat, evt.latlng.lng);
    });
  }

  async function bootstrap() {
    initMap();
    setupEvents();

    const loadingEl = document.getElementById("loading-overlay");
    const loadingStatus = document.getElementById("loading-status");

    try {
      loadingStatus.textContent = "Loading network metadata and travel-time grids…";
      const [meta] = await Promise.all([
        fetchJson("/api/meta"),
        ensureGrid("transit"),
        ensureGrid("walk"),
        ensureGrid("bike"),
        ensureIsochrones("transit"),
      ]);
      appState.meta = meta;

      document.getElementById("stats-summary").textContent =
        `${meta.total_crossroads.toLocaleString()} crossroads · ` +
        `${meta.rail_metro_tram_stations.toLocaleString()} rail/métro/tram · ` +
        `${meta.bus_stops.toLocaleString()} bus stops`;

      document.getElementById("modal-stats-box").textContent =
        `Origin: ${meta.origin_address} | ` +
        `Crossroads: ${meta.total_crossroads.toLocaleString()} | ` +
        `Stations: ${meta.total_stations.toLocaleString()} | ` +
        `Built: ${meta.built_at_utc}`;

      updateLegend();
      appState.heatmapLayer.redraw();
      await Promise.all([renderIsochrones(), renderStations()]);

      // Preload landmark inspection on Châtelet? Leave clean prompt until click, or keep ready
    } catch (err) {
      loadingStatus.textContent = `Failed to load transport data: ${err.message}`;
      return;
    }

    loadingEl.classList.add("hidden");
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();

