# The Lake Atlas

A client-facing, single-page bathymetric atlas for **Deeper Pro 2** sonar data. It parses a sonar CSV in the browser, generates a lake perimeter and depth surface, places an aeration-fountain marker at the deepest sounding, draws a medicinal-herb buffer around the shoreline, and overlays a county parcel boundary.

Everything runs locally. The optional Flask server reads the survey files from disk, computes summary statistics in Python, and asks DeepSeek for three stewardship notes. Only aggregate statistics are sent to DeepSeek — never raw coordinates, parcel attributes, or owner names. The browser can also run fully standalone with manual uploads and no server.

```
lake-atlas/
├── app.py                    Flask server + DeepSeek integration
├── mock_sonar_generator.py   Shapefile → mock Deeper CSV
├── fetch_parcel.py           County ArcGIS parcel fetcher → GeoJSON
├── requirements.txt          Flask, openai, python-dotenv
├── .env.example              Environment variable template
├── data/
│   ├── pond_boundary.shp     Input shapefile (+ .shx and .dbf)
│   ├── mock_sonar_survey.csv Generator output / real sonar CSV
│   └── parcel.geojson        Parcel fetcher output
├── static/
│   └── index.html            Single-file app: UI, Mapbox, Turf, PapaParse
└── README.md                 This guide
```

---

## 1. Quick start

### Run the automated server (recommended)

```powershell
cd C:\Users\jrowl\Documents\lake-atlas
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "sk-your-key-here"
.\.venv\Scripts\python.exe app.py
```

Open <http://127.0.0.1:5000/>. The page automatically loads `data/mock_sonar_survey.csv`, `data/parcel.geojson`, and the DeepSeek notes. The **Mapbox public token** is still entered in the app's welcome dialog; data loads in the background before the map connects.

See [section 2](#2-automated-local-server-flask--deepseek) for `.env` setup, the API contract, and privacy details.

### Run the frontend alone

The app is still a single file. To use it without the Flask server, serve `static/` and upload files manually:

```powershell
cd C:\Users\jrowl\Documents\lake-atlas
python -m http.server 8000 --directory static
```

Then open <http://localhost:8000/>. Without `/api/lake-data`, the app keeps its manual upload flow and the built-in sample. Opening `static/index.html` directly may work in some browsers, but a local server avoids `file://` origin restrictions.

### Add a Mapbox token

The app deliberately ships without an API key. On first load it asks for a **public** token (`pk.…`).

1. Sign in at <https://account.mapbox.com/access-tokens/>.
2. Create a public token. The basemap needs `styles:tiles`, `styles:read`, `fonts:read`, and `vision:read`.
3. Restrict the token to your deployment URL, for example `http://localhost:5000/*` during development and `https://atlas.yourdomain.com/*` in production.
4. Paste it into the welcome dialog. It is stored in this browser's `localStorage`.

The token can also be supplied for a single session with `?token=pk...` in the URL; the app removes it from the address bar after reading it. A secret token (`sk.…`) is rejected — never use a secret token in client-side code.

### What loads immediately

- **Through Flask:** the server CSV, parcel, and DeepSeek notes replace the demo automatically on page load.
- **Standalone:** if there is no user data, the app loads a clearly labeled fictional sample — **Larkspur Pond** and **Larkspur Preserve** — so the full visualization is visible before a real survey. Any CSV upload replaces the sample. Use **Clear survey** to remove both.

---

## 2. Automated local server (Flask + DeepSeek)

`app.py` is a small Flask application that ties the three local steps together:

1. Read `data/mock_sonar_survey.csv` and `data/parcel.geojson`.
2. Compute summary statistics in Python.
3. Send **only those aggregates** to DeepSeek for three notes.
4. Return one JSON payload to `static/index.html`, which loads it automatically.

### Install

```powershell
cd C:\Users\jrowl\Documents\lake-atlas
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use `.\.venv\Scripts\python.exe ...` directly if PowerShell blocks `Activate.ps1`.

`requirements.txt` installs Flask, the official `openai` Python SDK (used against DeepSeek's OpenAI-compatible endpoint), and `python-dotenv`. The mock generator needs **GeoPandas** separately:

```powershell
.\.venv\Scripts\python.exe -m pip install geopandas
```

### Set the DeepSeek API key

The key belongs in the **server environment**, never in `static/index.html`, `app.py`, the JSON response, or source control.

**Option A — current PowerShell session:**

```powershell
$env:DEEPSEEK_API_KEY = "sk-your-deepseek-key-here"
```

**Option B — persistent user environment (new terminal required):**

```powershell
setx DEEPSEEK_API_KEY "sk-your-deepseek-key-here"
```

**Option C — local `.env` file (easiest for repeat use):**

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace the placeholder in `.env`. The file is listed in `.gitignore`; do not commit it or paste a real key into `.env.example`.

Optional variables:

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek model name |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `DEEPSEEK_TIMEOUT` | `45` | Request timeout in seconds |
| `NARRATIVE_CACHE_SECONDS` | `3600` | In-memory narrative cache lifetime; `0` disables |
| `LAKE_ATLAS_HOST` | `127.0.0.1` | Flask bind address |
| `LAKE_ATLAS_PORT` | `5000` | Flask port |
| `LAKE_ATLAS_DATA_DIR` | `data/` | Alternate input directory |
| `FLASK_DEBUG` | `0` | `1` enables Flask debug mode |

### Start the server

```powershell
cd C:\Users\jrowl\Documents\lake-atlas
.\.venv\Scripts\python.exe app.py
```

Then open <http://127.0.0.1:5000/>. `GET /` serves `static/index.html`; assets load from `/static/`.

### Endpoint contract

**`GET /api/lake-data`** — returns the raw CSV, parsed GeoJSON, Python statistics, and the three narratives.

```json
{
  "generatedAt": "2026-11-06T20:45:00+00:00",
  "csv": "lat,lon,depth_m,fish_detected\n...",
  "csvFileName": "mock_sonar_survey.csv",
  "geojson": { "type": "FeatureCollection", "features": [ ... ] },
  "geojsonFileName": "parcel.geojson",
  "stats": {
    "soundingCount": 1200,
    "maxDepthMeters": 9.6,
    "meanDepthMeters": 2.97,
    "medianDepthMeters": 2.47,
    "fishContacts": 124,
    "fishContactRatePercent": 10.3,
    "surveyAreaAcres": 0.59,
    "shorelinePerimeterMeters": 225.5,
    "herbBufferSquareMeters": 1206.2,
    "parcelAcres": 37.955,
    "bounds": { "south": 39.469445, "west": -78.221893, "north": 39.470294, "east": -78.221417 }
  },
  "narratives": { "fish": "...", "aeration": "...", "herbs": "..." },
  "narrativeStatus": "generated",
  "warnings": []
}
```

- `narrativeStatus` is `generated`, `cached`, `skipped`, `partial`, or `error`.
- Add `?refresh=1` to bypass the narrative cache.
- If the key is missing or the DeepSeek request fails, the endpoint still returns `200` with the data and empty narrative strings. The app loads normally; `warnings` explains the issue.
- Missing input files return `503` with a JSON `error`. Malformed CSV/GeoJSON returns `422`.
- **`GET /api/health`** — reports file presence and whether a key is configured. It never returns the key.

**Survey area** is the convex hull of the valid soundings in metres, reported in m² and acres. It is the extent covered by the survey, not the parcel area. The shoreline and herb-buffer figures use that hull; `parcelAcres` reads a stated acreage attribute such as `CALC_ACRE` from the GeoJSON. The visible frontend shoreline uses a tighter concave hull, so its acreage may differ slightly.

### DeepSeek prompt and privacy

The server sends a facts-only prompt containing soundings, max/mean/median depth, depth percentiles, fish contacts, survey extent, buffer estimate, parcel acreage, and instructions against fabrication. It does **not** send latitude/longitude, owner names, addresses, parcel IDs, or CSV rows. DeepSeek is asked for strict JSON with `fish`, `aeration`, and `herbs` keys, each 90–130 words.

The narratives are cached in memory for an hour by default, keyed by a hash of the statistics and model. Restarting the server clears the cache.

### Automatic frontend load

The snippet at the bottom of `static/index.html` waits for `window.LakeAtlas`, calls `/api/lake-data`, pipes the CSV and GeoJSON through `LakeAtlas.loadData(...)`, then injects the notes:

```js
const response = await fetch('/api/lake-data', { cache: 'no-store' });
const payload = await response.json();

await window.LakeAtlas.loadData({
  csv: payload.csv,
  csvFileName: payload.csvFileName,
  geojson: payload.geojson,
  geojsonFileName: payload.geojsonFileName
});

window.LakeAtlas.setNarratives(payload.narratives);
```

`loadData` applies the parcel first, ingests the CSV once, runs a single spatial analysis, and does not clear the data if the map is not connected yet. If the server is down, the fetch is logged to the console and the app falls back to the sample/manual upload flow.

### Regenerating the inputs

```powershell
# 1. Mock sonar CSV from the pond shapefile (GeoPandas required)
.\.venv\Scripts\python.exe mock_sonar_generator.py
#    Defaults: data/pond_boundary.shp → data/mock_sonar_survey.csv

# 2. Parcel GeoJSON (standard-library only)
.\.venv\Scripts\python.exe fetch_parcel.py `
  --address "310 Red Gate Retreat, Berkeley Springs, WV" `
  --service-url "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer" `
  --layer 0
#    Default output: data/parcel.geojson

# 3. Restart or reload the app
```

### Data note

The checked-in sample CSV and parcel are spatially consistent: the survey soundings sit inside the 37.9-acre Berkeley Springs, WV parcel. Replace both together when you move to another lake, and confirm their coordinate extents overlap before trusting the combined analysis.

---

## 3. The National Park aesthetic

The interface uses warm paper, forest ink, oxidized clay, moss, double rules, a circular survey seal, USGS-flavored labels, and **Fraunces** serif display type. The map itself gets its vintage feel from two layers:

1. **Your Mapbox Studio style** — land, water, roads, labels, hillshade, and topographic linework.
2. **The app's treatment** — a warm paper wash, grain, cream map controls, custom depth raster, hand-drawn depth contours, a stitched park-boundary line, and a hatch-pattern herb buffer.

### Build the studio style

1. Open <https://studio.mapbox.com/> and create a new style from **Outdoors**. Outdoors already has road detail, park shading, and contour-friendly linework.
2. Set the broad palette. The exact layer names differ by style version; use the style editor's search and color controls.

   | Element | Suggested color | Notes |
   |---|---|---|
   | Land / background | `#E7DFCB` | warm paper |
   | Water | `#9FB7AA` | sage-teal, not bright blue |
   | Parks / vegetation | `#C9C39E` | muted olive |
   | Dense vegetation | `#A7B08C` | optional second green |
   | Primary roads | `#B8A78A` | tan |
   | Minor roads | `#CFC3AD` | lighter tan |
   | Labels | `#2C352C` | forest ink |
   | Label halo | `#F4EEDE` | paper |
   | Hillshade shadow | `#6B5D45` | warm brown, opacity around 18% |
   | Contour lines | `#A89C7E` | low opacity, keep them quiet |

3. Keep the **hillshade** and **contour** layers. The app adds its own 3D terrain DEM and a fallback hillshade, but it looks best when the Studio style has subtle relief already.
4. Avoid adding a high-contrast blue water fill or saturated road colors. The analysis raster needs to stay the loudest thing on the map.
5. Choose **Publish**, then **Share → Style URL**, and copy the value that looks like:

   ```
   mapbox://styles/your-user-name/ckxyz123...
   ```

6. In the app, open the settings gear and paste the URL into **Style URL**, then choose **Reconnect map**. The welcome dialog also has an **Advanced → Custom park style URL** field.

The app adds:

- `mapbox://mapbox.mapbox-terrain-dem-v1` if the style does not already have a raster DEM.
- A gentle warm fog/atmosphere.
- A hillshade layer if none exists in the style.
- All analysis layers beneath the first symbol layer, so base-map labels remain readable.

---

## 4. Sonar CSV format

The app auto-detects common Deeper/export column names. At minimum you need **latitude, longitude, and depth**. Fish detection is optional.

```csv
lat,lon,depth_m,fish_detected
44.30470,-68.24380,1.42,false
44.30455,-68.24361,3.65,true
44.30431,-68.24344,6.80,false
44.30418,-68.24377,7.20,true
```

### Accepted columns

| Field | Accepted header examples |
|---|---|
| Latitude | `lat`, `latitude`, `y`, `gps_lat`, `latitude_deg`, `lat_deg`, `latitude_dd` |
| Longitude | `lon`, `lng`, `long`, `longitude`, `x`, `gps_lon`, `longitude_deg`, `lng_deg`, `longitude_dd` |
| Depth | `depth`, `depth_m`, `depth_ft`, `depth_meters`, `depth_feet`, `z`, `bathymetry`, `water_depth` |
| Fish (optional) | `fish`, `fish_detected`, `fish_detection`, `fish_count`, `fish_activity`, `fish_school`, `fish_returns` |

Header matching is case-insensitive and tolerant of spaces, parentheses, and units. To add a custom alias, edit `FIELD_ALIASES` in the `<script>` section of `index.html`.

### Depth units

- If the depth header says `ft`, `feet`, or `foot`, values are treated as feet and converted to metres.
- If the values themselves contain `ft`/`feet`, auto-detection also treats them as feet.
- Otherwise depths default to metres.
- Use **Settings → Survey rendering → Sonar depth units** to override. Changing units reruns the local analysis without another upload.

Internally all depths are normalised to metres. Negative depth values are interpreted by magnitude; if your export uses elevation instead of water depth, convert it before upload.

### Coordinates

Use WGS84 decimal degrees: longitude first in GeoJSON, latitude and longitude in separate CSV columns. If your Deeper export is in a projected county coordinate system, or in degrees/minutes/seconds, convert it to WGS84 decimal degrees first.

### Fish handling

Fish cells accept booleans (`true`/`false`, `yes`/`no`), words such as `detected`, or numeric counts. Fish returns appear as warm clay circles on the map. If the column exists but every value is zero, the app still reports “0 contacts” rather than pretending the column is absent.

The parser skips invalid coordinate rows and tells you how many were rejected. Rows without a positive depth are retained for the map perimeter but do not contribute to interpolation.

### Large files

The depth surface is computed on a fixed local grid (`CONFIG.gridLongSide`, default 148 cells on the long side), not one polygon per sounding, so tens of thousands of points remain manageable. Fish markers are capped at 1,200 displayed contacts; the statistics still count all contacts.

---

## 5. Property boundary GeoJSON

Upload a `Polygon` or `MultiPolygon` GeoJSON. The app also accepts a single `Feature`, a `FeatureCollection`, or a `GeometryCollection` and extracts every polygon it finds.

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": { "name": "Larkspur Preserve", "PARCEL_ID": "12-34" },
      "geometry": {
        "type": "Polygon",
        "coordinates": [[
          [-68.2475, 44.3021],
          [-68.2400, 44.3021],
          [-68.2400, 44.3074],
          [-68.2475, 44.3074],
          [-68.2475, 44.3021]
        ]]
      }
    }
  ]
}
```

Properties are preserved and exported again. Common fields such as `name`, `property_name`, `situs`, `address`, `owner`, `parcel_id`, `apn`, and `pin` are used to label the parcel in the map plate.

A county `MultiPolygon` is fine. If you have many parcels in one file, the app imports them all; use `fetch_parcel.py` (below) to select a single parcel when that is what you want.

---

## 6. How the spatial analysis works

All of this happens in the browser with Turf.js and a small amount of canvas code.

### Lake perimeter — concave hull

`buildLakeHull()` in `static/index.html`:

1. Removes duplicate coordinates.
2. Estimates the median nearest-neighbour spacing from a sample of soundings.
3. Tries Turf's `concave()` with progressively looser `maxEdge` lengths based on that spacing.
4. Falls back to a convex hull if a connected shoreline cannot be built.
5. Runs two Chaikin corner-cutting passes to remove pixel-like sawteeth.

A warning and toast appear when the convex fallback is used. The fallback usually means soundings do not wrap the shoreline closely enough; add more perimeter returns for a better hull.

### Bathymetry — inverse-distance weighting

`buildDepthGrid()`:

- Converts the hull's bounding box into a local metric grid using a latitude-aware longitude scale.
- Puts every sounding in a spatial hash bucket.
- For each grid-cell centre, finds the nearest 12 soundings and computes inverse-distance weighting with power `2.25`:

  ```
  depth = Σ(depthᵢ / dᵢ^p) / Σ(1 / dᵢ^p)
  ```

- A direct hit within 0.55 m wins outright.
- Cells inside the shoreline contribute to the area-weighted mean depth.
- The grid is clipped to the smoothed hull for the raster and used again for contours.

Tuning knobs are in `CONFIG` near the top of the script:

```js
const CONFIG = {
  gridLongSide: 148,    // more cells = more detail, more CPU
  idwPower: 2.25,       // lower = smoother; higher = more local
  idwNeighbours: 12,    // nearest soundings per cell
  contourTarget: 5,     // approximate number of depth lines
  // ...
};
```

### Depth raster

The values are colored through the single-hue teal/sage ramp in `DEPTH_RAMP` (pale shallows → dark deep water), drawn to a small canvas, smoothly upscaled, then masked with the hull path so no rectangle leaks outside the lake. The result is a Mapbox image source.

### Contours

`buildContourFeatures()` runs marching squares across the grid, generates line segments at nice depth intervals, and discards segments whose midpoint is outside the hull. A cream “glow” line sits under the darker contour line to keep it legible over the raster.

### Fountain placement

The fountain uses the **single deepest positive CSV sounding** — not an interpolated cell. That keeps the marker tied to a real field observation. It is rendered as a custom DOM marker with an inline SVG and a popup. The export includes the point, depth, and method.

The deepest point is a defensible starting point for aeration, not a full water-quality siting model. Verify mixing, access, power, permits, and safety in the field.

### Medicinal-herb buffer

```js
const outer = turf.buffer(hull, meters, { units: 'meters', steps: 28 });
// Turf 7's difference() takes one FeatureCollection of two or more polygons.
const ring = turf.difference(turf.featureCollection([outer, hull]));
```

If `difference()` returns no usable polygon for an unusually complex
shoreline, the app falls back to a ring polygon built from the buffered
outline with the lake as its hole, so the planting zone is still rendered,
measured, and exported.

The default is **5 metres outside the generated hull**. Change it in two places:

- `CONFIG.defaultBufferMeters` (the fallback/default), and
- **Settings → Survey rendering → Herb buffer** (2–30 m, reruns live).

The buffer is a planning overlay, not a legal setback, easement, or planting permit. It is styled with translucent sage, a diagonal herb hatch, and a dashed outline. Its area is shown in the measurements strip and in the AI brief.

### Fish contacts

Only displayed when the CSV has a fish column with positive detections. Circle size scales with the detected count. Clicking a contact opens its depth and count.

---

## 7. Lake Care & Ecology Summary

The three editable fields — **Fish Management**, **Aeration Benefits**, and **Herb Cultivation** — start with data-aware placeholders. The placeholders change when you load a new survey and never overwrite anything you have typed.

### Editable notes

- Click any field and type. It is a plain-text `contenteditable` region.
- Content is held in memory for the session.
- Use **Copy AI brief** to put a structured, measurement-rich prompt on the clipboard.

The brief includes survey/property names, center, soundings, area, shoreline, max/mean depth, fish detections, fountain candidate, buffer size and area, parcel area, and a no-fabrication instruction. It also includes any text already in the three fields.

### Injecting LLM-generated text

For a host page, automation, or a custom integration, use the public API:

```html
<script>
  // Safe: the app writes this through textContent, never innerHTML.
  window.LakeAtlas.setNarrative(
    'fish',
    'The sonar survey recorded shallow-water contacts along the western shelf...'
  );
</script>
```

Available keys: `fish`, `aeration`, `herbs`.

### Loading data programmatically

`LakeAtlas.loadData` accepts the same payload the Flask endpoint returns:

```js
await LakeAtlas.loadData({
  csv: csvText,                       // required raw CSV string
  csvFileName: 'mock_sonar_survey.csv',
  geojson: parcelGeoJson,             // parsed GeoJSON object, optional
  geojsonFileName: 'parcel.geojson'
});
```

It applies the parcel first, ingests the CSV once, and runs a single analysis when the map is ready. It returns `getSummary()`. Use it with the automatic loader in [section 2](#2-automated-local-server-flask--deepseek), or from automation of your own.

```js
// One call for all three care sections. Text is written with textContent.
LakeAtlas.setNarratives({
  fish: '...',
  aeration: '...',
  herbs: '...'
});
```

```js
// Structured current measurements, suitable for an LLM prompt.
const summary = LakeAtlas.getSummary();

// Other helpers.
LakeAtlas.getNarrative('herbs');
LakeAtlas.refresh();       // rerun local analysis with current settings
LakeAtlas.fitToSurvey();   // recenter the map
LakeAtlas.loadSample();    // load the built-in sample
```

The optional Flask server in `app.py` owns the only model call. Its DeepSeek key stays in the server environment and is never exposed to this HTML. When the key is absent, the page still loads the server data and leaves the care notes for manual entry or the AI brief.

### Exporting analysis

**GeoJSON** downloads a derived-feature file containing:

- the uploaded parcel boundary,
- the smoothed lake hull,
- the herb buffer,
- the fountain point,
- every depth-contour line,
- every fish contact.

The file includes metadata and EPSG:4326 coordinates. Uploaded raw soundings are not included.

---

## 8. Fetching a county parcel boundary

`fetch_parcel.py` is a separate, standard-library-only Python 3 tool. It queries an **ArcGIS REST MapServer or FeatureServer**, selects the parcel containing a coordinate (or a nearby parcel when the point falls just outside), and writes GeoJSON ready for the app.

There is no single national parcel endpoint in the U.S. Parcel data is fragmented by county and sometimes by municipality, so the script cannot guess the right service. You must supply the county's ArcGIS service URL.

### Find the service

Try one of these:

- Search `"<county name> GIS parcel REST"` or `"<county name> ArcGIS REST services"`.
- Browse the county's ArcGIS REST directory, often at a URL such as `https://gis.county.gov/arcgis/rest/services`.
- Look for a folder named `Parcels`, `Cadastral`, `Tax`, or `Property`.
- Many public services end in `/MapServer` or `/FeatureServer`, with layers at `/MapServer/0`, `/FeatureServer/12`, and so on.

### List the layers first

```powershell
python fetch_parcel.py `
  --service-url "https://gis.examplecounty.gov/arcgis/rest/services/Parcels/MapServer" `
  --list-layers
```

Output looks like:

```
Service: Parcels
URL: https://gis.examplecounty.gov/arcgis/rest/services/Parcels/MapServer
   0: Tax Parcels [esriGeometryPolygon]
   1: Parcel Labels [esriGeometryPolygon]
```

Pass `--layer 0` when auto-detection is not obvious. The auto-picker prefers polygon layers whose names include *parcel*, *tax*, *cadastral*, *property*, *land*, or *lot*, and penalizes annotation, road, easement, zoning, historical/retired, air-rights, and other non-parcel layers. County services often contain several plausible layers; a direct layer URL such as `/MapServer/39` is the most reliable choice.

### Address lookup

The script uses the U.S. Census oneline-address geocoder (no key required):

```powershell
python fetch_parcel.py `
  --address "123 Main St, Anytown, ME 04000" `
  --service-url "https://gis.examplecounty.gov/arcgis/rest/services/Parcels/MapServer" `
  --out ".\data\parcel.geojson"
```

### Coordinate lookup

```powershell
python fetch_parcel.py `
  --lat 44.3047 --lon -68.2438 `
  --service-url "https://gis.examplecounty.gov/arcgis/rest/services/Parcels/FeatureServer/0" `
  --search-radius 30 `
  --out ".\data\parcel.geojson"
```

### Useful options

| Option | Purpose |
|---|---|
| `--service-url` | Service root or direct layer URL. Defaults to `ARCGIS_PARCEL_URL`. |
| `--layer 0` / `--layer "tax.*parcel"` | Choose a layer by id or regex. |
| `--where "TAXCODE='R1'"` | Add a county SQL filter. Use quoting appropriate to your shell. |
| `--search-radius 50` | Envelope radius in metres around the point. Default 25. |
| `--all` | Export all intersecting parcels, not just the selected one. |
| `--token` | ArcGIS token for a secured service. Defaults to `ARCGIS_TOKEN`. |
| `--timeout 60` | Network timeout in seconds. |
| `--out -` | Write GeoJSON to stdout (summary goes to stderr). |

The script sends an envelope query with `inSR=4326`, `outSR=4326`, `spatialRel=esriSpatialRelIntersects`, and `returnGeometry=true`. It strips Z/M values, closes rings, fixes GeoJSON winding, and assembles holes and islands into valid `Polygon`/`MultiPolygon` geometry.

The output includes:

- every original county attribute,
- a normalized `name` when it can infer an address/parcel/owner label,
- top-level metadata recording the service, layer, point, radius, geocoder, selection method, and timestamp,
- a disclaimer that parcel boundaries are planning data, not a legal survey.

### Tokens and access

Some county services allow anonymous queries; others require a token. Use:

```powershell
$env:ARCGIS_TOKEN = "your-short-lived-token"
python fetch_parcel.py --lat ... --lon ... --service-url "..."
```

or `--token`. Tokens are redacted from error output and never written to the GeoJSON. The script uses GET requests, so the token may still appear in network or proxy logs — prefer a short-lived, least-privilege token.

### Then load it

The default output is `data/parcel.geojson`; the Flask route picks it up automatically on the next page load. To use the standalone frontend, choose **Parcel boundary → Choose GeoJSON** and select the file. The boundary is drawn with a cream halo and a stitched clay park-border line.

---

## 9. Customization reference

All app tuning lives near the top of the `<script>` block in `static/index.html`.

### Key constants

```js
CONFIG.defaultBufferMeters   // 5 — herb ring width
CONFIG.defaultTerrain        // 1.35 — initial 3D exaggeration
CONFIG.gridLongSide          // 148 — IDW raster resolution
CONFIG.idwPower              // 2.25
CONFIG.idwNeighbours         // 12
CONFIG.contourTarget         // 5
CONFIG.maxFishMarkers        // 1200 displayed contacts
FIELD_ALIASES                // CSV header aliases
DEPTH_RAMP                   // sequential teal ramp
LAYER_IDS / SOURCE_IDS       // map layer naming
```

### Styling

The palette is defined as CSS custom properties at the top of `<style>` (`--paper`, `--forest`, `--clay`, etc.). The map controls, popups, legend, sidebar, and dialogs all reference those tokens. The depth raster uses `DEPTH_RAMP` in JavaScript, not CSS — keep the two in sync if you change the depth colors.

### Mapbox token and style storage

- `lake-atlas.token.v1`
- `lake-atlas.style.v1`
- `lake-atlas.settings.v1`

These are `localStorage` keys. Use the settings drawer to change them. Clear site data to forget the token.

### Analysis layer order

Property fill → lake fill → herb fill/hatch/outline → depth raster → contour glow/contours → shoreline halo/line → fish contacts → property background/line. All custom layers are inserted before the style's first symbol layer, so base labels stay on top.

---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Map stays on “Preparing the atlas” | Token missing/restricted, custom style URL wrong, or network blocked. After 18 seconds the app returns to the access dialog with an error. |
| 401/403 map error | Token restricted to another URL, missing scopes, or a secret token used in the browser. Recreate a public token for your deployed origin. |
| Custom style does not load | The style must be published and the URL must start with `mapbox://styles/`. Leave it blank to test against Outdoors. |
| “Concave fallback” warning | Soundings do not wrap the shoreline. Add near-shore returns or use a planned survey grid that covers the perimeter. |
| CSV column error | A header is missing or uses an unusual name. Add an alias in `FIELD_ALIASES`, or rename the column. |
| Depths look 3.28× too large | The file is in feet but is being read as metres. Set **Depth units → Feet**, or rename the header to `depth_ft`. |
| Fish panel says no column | No recognized fish header was found. Add `fish`/`fish_detected`/`fish_count`, or accept the blank state. |
| No parcel line | The GeoJSON contains no Polygon/MultiPolygon, or is in a projected CRS. Re-export in EPSG:4326. |
| ArcGIS “no parcel-like layer” | Use `--list-layers` and `--layer <id or regex>`. |
| ArcGIS returns no features | Increase `--search-radius`; verify the point is inside the layer extent; relax `--where`; confirm the layer is public. |
| ArcGIS token errors | Supply `--token` / `ARCGIS_TOKEN`; check expiry and layer permissions. |
| Map controls covered on mobile | The bottom field-notes sheet intentionally floats above the map; Mapbox attribution/logo are moved above the closed sheet with CSS. Do not hide attribution in production. |
| WebGL unavailable | Use a current Chrome, Edge, Safari, or Firefox with hardware acceleration enabled. |
| Server says `Missing mock_sonar_survey.csv` | Run the generator or copy a real CSV into `data/`. Check `GET /api/health` for which file is absent. |
| Care notes are blank | The key is missing, invalid, or the request failed. Check `GET /api/lake-data` → `warnings` and `narrativeStatus`; the data itself still loads. |
| Key changes are ignored | Environment variables are read at server start. Restart `app.py` after changing `.env` or `setx`. |
| `Port 5000 already in use` | Set `$env:LAKE_ATLAS_PORT = "5055"` before starting, or stop the other process. |
| Browser shows the demo instead of server data | The page was served by `python -m http.server`, not Flask, so `/api/lake-data` does not exist. Start `app.py` and use its URL. |

---

## 11. Privacy, accuracy, and operating notes

- **Raw sonar and boundary files stay local.** In the standalone app they live only in the browser session. With `app.py`, the server reads `data/` on this machine and returns the CSV/GeoJSON to the local page. Only aggregate statistics are sent to DeepSeek; coordinates, owner names, addresses, and parcel IDs are never included in the prompt. Without `DEEPSEEK_API_KEY`, no model request is made.
- **The DeepSeek key is server-side only.** Keep `DEEPSEEK_API_KEY` in the environment or `.env`; never place it in `static/index.html`, a response payload, or source control.
- **The DeepSeek notes are drafts.** They are generated from aggregate metrics only and can be generic or wrong about site conditions. Review and edit them before client delivery; they are not a substitute for a site visit, water-quality testing, or professional advice.
- **Mapbox sees basemap requests**, including the map viewport and token. Review Mapbox's terms and configure a restricted public token.
- **The map is not a navigational chart.** Interpolated depths between sonar soundings can be wrong. Do not use this for navigation, dredging, structural design, or safety-critical decisions.
- **The herb buffer and fountain site are planning aids.** Verify property lines, setbacks, utilities, permits, access, and water-quality conditions with the county and a qualified professional.
- **Parcel data is not a legal survey.** County GIS layers carry their own accuracy disclaimers and positional error. Check recorded deeds and surveys.
- **Keep Mapbox attribution and logo visible.** The app places them above the mobile sheet rather than hiding them.
- **Aeration fountain placement** is based only on the deepest field sounding. A real design should consider mixing volume, oxygen demand, stratification, power access, maintenance, anchoring, and fish habitat.

### Browser support

Modern evergreen browsers with WebGL and `contenteditable` support: Chrome/Edge 110+, Safari 17+, Firefox 120+. The app uses `inert` on collapsed care cards and falls back gracefully if clipboard access is unavailable. Reduced-motion preferences are respected.

### Third-party versions

The app pins:

- Mapbox GL JS `3.8.0`
- Turf.js `7.2.0`
- PapaParse `5.5.3`
- Google Fonts: Fraunces, Karla, Barlow Condensed

To update a dependency, change its `<script>`/`<link>` URL and test the map, parsing, and spatial analysis again.
