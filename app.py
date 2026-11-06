"""Lake Atlas local server.

Serves the single-page atlas and exposes one data endpoint that bundles:

    * the raw sonar CSV,
    * the parsed parcel GeoJSON,
    * summary statistics computed in Python, and
    * three DeepSeek-generated stewardship narratives.

Run:
    python app.py

The DeepSeek key is read from the DEEPSEEK_API_KEY environment variable (or a
local .env file). It is never written to a response, log line, or static file.
Only aggregate statistics are sent to DeepSeek; raw coordinates, owner names,
and parcel attributes stay on this machine.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from flask import Flask, jsonify, request, send_from_directory

try:  # Optional: lets a local .env file hold the key during development.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is optional at runtime.
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_STATIC_DIR = BASE_DIR / "static"

CSV_FILE_NAME = "mock_sonar_survey.csv"
GEOJSON_FILE_NAME = "parcel.geojson"

CARE_KEYS = ("fish", "aeration", "herbs")

LAT_ALIASES = ("lat", "latitude", "y", "gps_lat", "latitude_deg", "lat_deg", "latitude_dd", "lat_dd")
LON_ALIASES = ("lon", "lng", "long", "longitude", "x", "gps_lon", "gps_lng", "longitude_deg", "lon_deg", "lng_deg", "longitude_dd", "lon_dd")
DEPTH_ALIASES = ("depth", "depth_m", "depthm", "depth_meters", "depth_metres", "depth_ft", "depthft", "depth_feet", "z", "bathymetry", "water_depth")
FISH_ALIASES = ("fish", "fish_detected", "fish_detection", "fish_count", "fishcount", "fish_activity", "fish_school", "fish_returns", "fish_targets")

TRUTHY = {"true", "yes", "y", "1", "detected", "fish", "present", "school"}


class ApiError(Exception):
    """Expected client-facing failure with an HTTP status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SonarRow:
    lat: float
    lon: float
    depth_m: float
    fish: float


@dataclass(frozen=True)
class SonarSurvey:
    rows: tuple[SonarRow, ...]
    rejected: int
    has_fish_field: bool
    depth_unit: str


# ---------------------------------------------------------------------------
# CSV parsing and statistics
# ---------------------------------------------------------------------------


def normalize_header(value: Any) -> str:
    text = str(value or "").replace("﻿", "").lower().strip()
    text = re.sub(r"[()[\]{}]", " ", text)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def pick_field(headers: Iterable[str], aliases: Sequence[str]) -> str | None:
    normalized = [(header, normalize_header(header)) for header in headers]
    for alias in aliases:
        for header, key in normalized:
            if key == alias:
                return header
    for alias in aliases:
        for header, key in normalized:
            if key.startswith(alias + "_") or key.endswith("_" + alias):
                return header
    return None


def parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    if value is None:
        return None
    text = str(value).strip()
    if not text or re.fullmatch(r"(n/?a|nan|null|none|—|-|--)", text, re.IGNORECASE):
        return None
    text = re.sub(r"\s*(m|meters|metres|ft|feet|foot)\s*$", "", text, flags=re.IGNORECASE)
    text = text.replace(",", ".")
    text = re.sub(r"[^0-9eE+\-.]", "", text)
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_fish(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().lower()
    if not text:
        return 0.0
    if text in TRUTHY:
        return 1.0
    number = parse_number(value)
    return number if number is not None and number > 0 else 0.0


def parse_sonar_csv(text: str) -> SonarSurvey:
    if not text.strip():
        raise ApiError(f"{CSV_FILE_NAME} is empty.", 422)

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), skipinitialspace=True)
    headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
    if len(headers) < 3:
        raise ApiError(
            "The sonar CSV needs a header row with latitude, longitude, and depth columns.",
            422,
        )

    lat_field = pick_field(headers, LAT_ALIASES)
    lon_field = pick_field(headers, LON_ALIASES)
    depth_field = pick_field(headers, DEPTH_ALIASES)
    fish_field = pick_field(headers, FISH_ALIASES)

    if not lat_field or not lon_field:
        raise ApiError(f"Could not find latitude/longitude columns in {CSV_FILE_NAME}.", 422)
    if not depth_field:
        raise ApiError(f"Could not find a depth column in {CSV_FILE_NAME}.", 422)

    depth_key = normalize_header(depth_field)
    raw_rows = list(reader)
    depth_samples = [
        str(raw.get(depth_field) or "")
        for raw in raw_rows[:50]
        if raw.get(depth_field) is not None
    ]
    looks_feet = bool(re.search(r"(^|_)ft($|_)|feet|foot", depth_key)) or any(
        re.search(r"\b(ft|feet|foot)\b", value, re.IGNORECASE)
        for value in depth_samples
    )
    unit = "ft" if looks_feet else "m"
    factor = 0.3048 if unit == "ft" else 1.0

    rows: list[SonarRow] = []
    rejected = 0
    for raw in raw_rows:
        lat = parse_number(raw.get(lat_field))
        lon = parse_number(raw.get(lon_field))
        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            rejected += 1
            continue

        raw_depth = parse_number(raw.get(depth_field))
        depth = abs(raw_depth) * factor if raw_depth is not None else 0.0
        rows.append(
            SonarRow(
                lat=lat,
                lon=lon,
                depth_m=depth,
                fish=parse_fish(raw.get(fish_field)) if fish_field else 0.0,
            )
        )

    if len(rows) < 3:
        raise ApiError(
            f"Only {len(rows)} valid coordinate row(s) were found in {CSV_FILE_NAME}.",
            422,
        )
    if not any(row.depth_m > 0 for row in rows):
        raise ApiError(f"No positive depth values were found in {CSV_FILE_NAME}.", 422)

    return SonarSurvey(
        rows=tuple(rows),
        rejected=rejected,
        has_fish_field=bool(fish_field),
        depth_unit=unit,
    )


def local_xy(lat: float, lon: float, origin_lat: float) -> tuple[float, float]:
    """Equirectangular projection to metres, accurate at pond scale."""
    x = math.radians(lon) * 6_378_137.0 * math.cos(math.radians(origin_lat))
    y = math.radians(lat) * 6_378_137.0
    return x, y


def convex_hull(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew monotone chain; returns counter-clockwise hull vertices."""
    unique = sorted(set(points))
    if len(unique) <= 1:
        return unique

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def polygon_area(ring: Sequence[tuple[float, float]]) -> float:
    return abs(sum(
        ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
        for i in range(len(ring))
    )) / 2.0


def polygon_perimeter(ring: Sequence[tuple[float, float]]) -> float:
    return sum(
        math.dist(ring[i], ring[(i + 1) % len(ring)])
        for i in range(len(ring))
    )


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def parcel_acres(geojson: Mapping[str, Any]) -> float | None:
    """Read a stated acreage from common parcel attributes when present."""
    candidate_keys = {
        "calc_acre", "calc_acres", "gis_acres", "acres", "acreage",
        "shape_area", "shapearea", "area_acres",
    }
    total = 0.0
    found = False
    for feature in geojson.get("features") or []:
        properties = feature.get("properties") or {}
        for key, value in properties.items():
            if normalize_header(key) not in candidate_keys:
                continue
            number = parse_number(value)
            if number is not None and number > 0:
                total += number
                found = True
                break
    return round(total, 3) if found else None


def summarize_survey(survey: SonarSurvey, geojson: Mapping[str, Any]) -> dict[str, Any]:
    depths = [row.depth_m for row in survey.rows if row.depth_m > 0]
    fish_contacts = sum(1 for row in survey.rows if row.fish > 0)
    origin_lat = sum(row.lat for row in survey.rows) / len(survey.rows)
    xy = [local_xy(row.lat, row.lon, origin_lat) for row in survey.rows]
    hull = convex_hull(xy)

    area_m2 = polygon_area(hull) if len(hull) >= 3 else None
    perimeter_m = polygon_perimeter(hull) if len(hull) >= 3 else None
    buffer_m = 5.0
    buffer_area_m2 = (
        perimeter_m * buffer_m + math.pi * buffer_m ** 2
        if perimeter_m is not None
        else None
    )

    lats = [row.lat for row in survey.rows]
    lons = [row.lon for row in survey.rows]

    def pct_at_or_below(threshold: float) -> float | None:
        return round(100.0 * sum(1 for d in depths if d < threshold) / len(depths), 1) if depths else None

    return {
        "soundingCount": len(survey.rows),
        "rejectedRows": survey.rejected,
        "depthUnit": "m",
        "maxDepthMeters": round(max(depths), 2) if depths else None,
        "meanDepthMeters": round(sum(depths) / len(depths), 2) if depths else None,
        "medianDepthMeters": round(percentile(depths, 0.5), 2) if depths else None,
        "depthP10Meters": round(percentile(depths, 0.10), 2) if depths else None,
        "depthP90Meters": round(percentile(depths, 0.90), 2) if depths else None,
        "shallowerThan1_5mPercent": pct_at_or_below(1.5),
        "deeperThan3mPercent": round(
            100.0 * sum(1 for d in depths if d >= 3.0) / len(depths), 1
        ) if depths else None,
        "fishContacts": fish_contacts if survey.has_fish_field else None,
        "fishContactRatePercent": round(100.0 * fish_contacts / len(survey.rows), 1) if survey.has_fish_field and survey.rows else None,
        "hadFishField": survey.has_fish_field,
        "surveyAreaSquareMeters": round(area_m2, 1) if area_m2 is not None else None,
        "surveyAreaAcres": round(area_m2 / 4046.8564224, 2) if area_m2 is not None else None,
        "shorelinePerimeterMeters": round(perimeter_m, 1) if perimeter_m is not None else None,
        "herbBufferMeters": buffer_m,
        "herbBufferSquareMeters": round(buffer_area_m2, 1) if buffer_area_m2 is not None else None,
        "parcelAcres": parcel_acres(geojson),
        "bounds": {
            "south": min(lats),
            "west": min(lons),
            "north": max(lats),
            "east": max(lons),
        },
    }


# ---------------------------------------------------------------------------
# DeepSeek narratives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str
    model: str
    base_url: str
    timeout: float
    cache_seconds: int

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


_narrative_cache: dict[str, tuple[float, dict[str, str]]] = {}
_cache_lock = threading.Lock()


def _cache_key(stats: Mapping[str, Any], model: str) -> str:
    payload = json.dumps({"stats": stats, "model": model}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cached_narratives(key: str, ttl: int) -> dict[str, str] | None:
    if ttl <= 0:
        return None
    with _cache_lock:
        entry = _narrative_cache.get(key)
        if not entry:
            return None
        stored_at, narratives = entry
        if time.monotonic() - stored_at > ttl:
            _narrative_cache.pop(key, None)
            return None
        return dict(narratives)


def _store_narratives(key: str, narratives: Mapping[str, str]) -> None:
    with _cache_lock:
        if len(_narrative_cache) >= 32:
            oldest = min(_narrative_cache, key=lambda k: _narrative_cache[k][0])
            _narrative_cache.pop(oldest, None)
        _narrative_cache[key] = (time.monotonic(), dict(narratives))


def _metric_line(label: str, value: Any, suffix: str = "") -> str:
    return f"- {label}: {'not available' if value is None else f'{value}{suffix}'}"


def build_narrative_prompt(stats: Mapping[str, Any]) -> str:
    """Build a facts-only prompt. No coordinates, owner names, or raw rows."""
    fish = stats.get("fishContacts")
    fish_rate = stats.get("fishContactRatePercent")
    fish_text = (
        f"{fish} contacts across {stats['soundingCount']} soundings ({fish_rate}%)"
        if fish is not None
        else "no fish-detection column in the survey"
    )
    facts = "\n".join([
        _metric_line("Soundings", stats.get("soundingCount")),
        _metric_line("Maximum depth", stats.get("maxDepthMeters"), " m"),
        _metric_line("Mean depth", stats.get("meanDepthMeters"), " m"),
        _metric_line("Median depth", stats.get("medianDepthMeters"), " m"),
        _metric_line("10th-percentile depth", stats.get("depthP10Meters"), " m"),
        _metric_line("90th-percentile depth", stats.get("depthP90Meters"), " m"),
        _metric_line("Water shallower than 1.5 m", stats.get("shallowerThan1_5mPercent"), "%"),
        _metric_line("Water 3 m or deeper", stats.get("deeperThan3mPercent"), "%"),
        f"- Fish detections: {fish_text}",
        _metric_line("Mapped survey extent", stats.get("surveyAreaAcres"), " acres"),
        _metric_line("Estimated shoreline perimeter", stats.get("shorelinePerimeterMeters"), " m"),
        _metric_line("5 m herb-buffer area", stats.get("herbBufferSquareMeters"), " m²"),
        _metric_line("Parcel acreage", stats.get("parcelAcres"), " acres"),
    ])

    return f"""Survey summary:
{facts}

Write exactly three client-ready stewardship notes for this pond:

1. "fish" — Fish Management
2. "aeration" — Aeration Benefits
3. "herbs" — Herb Cultivation

Rules:
- Use only the metrics above. Do not invent species, water chemistry, depth readings, legal constraints, or ownership details.
- Each note must be 90-130 words of plain prose, no heading, no markdown, no bullet lists.
- Be specific about how the metrics shape the recommendation, and name any follow-up measurement that is needed before acting.
- Fish Management: address sampling coverage, depth/cover implications, and monitoring priorities.
- Aeration Benefits: explain why the deepest basin is a candidate, expected mixing/oxygen benefits, and caveats about sizing and stratification.
- Herb Cultivation: discuss the 5 m upland buffer, parcel context, site verification, and cautious stewardship. Do not promise that any species will thrive and do not give foraging or medical advice.
- Return a JSON object with exactly these string keys: "fish", "aeration", "herbs".
"""


def _sanitize_error(error: Exception, api_key: str) -> str:
    message = f"{type(error).__name__}: {error}"
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return message[:300]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def request_narratives(
    stats: Mapping[str, Any],
    settings: DeepSeekSettings,
    refresh: bool = False,
) -> tuple[dict[str, str], str, list[str]]:
    """Return (narratives, status, warnings). Data loading never depends on this."""
    empty = {key: "" for key in CARE_KEYS}
    warnings: list[str] = []

    if not settings.configured:
        warnings.append(
            "DEEPSEEK_API_KEY is not set. Add it to the environment or a local .env "
            "file to generate the three stewardship notes."
        )
        return empty, "skipped", warnings

    cache_key = _cache_key(stats, settings.model)
    if not refresh:
        cached = _cached_narratives(cache_key, settings.cache_seconds)
        if cached is not None:
            return cached, "cached", warnings

    try:
        from openai import OpenAI
    except ImportError:
        warnings.append(
            "The openai package is not installed. Run "
            "'python -m pip install -r requirements.txt' to enable DeepSeek notes."
        )
        return empty, "skipped", warnings

    client = OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout,
        max_retries=1,
    )

    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a freshwater pond-management ecologist. You write concise, "
                        "careful field notes for a landowner. You never invent measurements or "
                        "species. You return only valid JSON."
                    ),
                },
                {"role": "user", "content": build_narrative_prompt(stats)},
            ],
            temperature=0.4,
            max_tokens=1400,
            response_format={"type": "json_object"},
        )
    except Exception as error:  # Network, auth, quota, rate limit, timeout.
        warnings.append(f"DeepSeek request failed ({_sanitize_error(error, settings.api_key)}).")
        return empty, "error", warnings

    content = response.choices[0].message.content if response.choices else ""
    try:
        payload = json.loads(_strip_code_fence(content or ""))
    except (TypeError, json.JSONDecodeError):
        warnings.append("DeepSeek returned a response that was not valid JSON.")
        return empty, "error", warnings

    narratives: dict[str, str] = {}
    if isinstance(payload, Mapping):
        payload = {str(key).strip().lower(): value for key, value in payload.items()}
    for key in CARE_KEYS:
        value = payload.get(key) if isinstance(payload, Mapping) else None
        narratives[key] = value.strip() if isinstance(value, str) else ""

    missing = [key for key, value in narratives.items() if not value]
    if missing:
        warnings.append(f"DeepSeek did not return text for: {', '.join(missing)}.")
        return narratives, "partial", warnings

    _store_narratives(cache_key, narratives)
    return narratives, "generated", warnings


# ---------------------------------------------------------------------------
# Data loading and Flask application
# ---------------------------------------------------------------------------


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ApiError(
            f"Missing {path.name}. Place it in {path.parent} and reload.",
            503,
        ) from None
    except UnicodeDecodeError:
        raise ApiError(
            f"{path.name} is not UTF-8 encoded. Re-export it as UTF-8 and reload.",
            422,
        ) from None
    except OSError as error:
        raise ApiError(f"Could not read {path.name}: {error}", 500) from None


def load_geojson(path: Path) -> dict[str, Any]:
    text = load_text(path)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ApiError(f"{path.name} is not valid JSON: {error.msg}.", 422) from None
    if not isinstance(payload, dict):
        raise ApiError(f"{path.name} must contain a GeoJSON object.", 422)
    if payload.get("type") not in {"FeatureCollection", "Feature", "GeometryCollection"}:
        raise ApiError(
            f"{path.name} does not look like GeoJSON (type is {payload.get('type')!r}).",
            422,
        )
    return payload


def load_and_summarize(data_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    csv_text = load_text(data_dir / CSV_FILE_NAME)
    geojson = load_geojson(data_dir / GEOJSON_FILE_NAME)
    stats = summarize_survey(parse_sonar_csv(csv_text), geojson)
    return csv_text, geojson, stats


def deepseek_settings() -> DeepSeekSettings:
    return DeepSeekSettings(
        api_key=(os.environ.get("DEEPSEEK_API_KEY") or "").strip(),
        model=(os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat").strip(),
        base_url=(os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip(),
        timeout=float(os.environ.get("DEEPSEEK_TIMEOUT") or 45),
        cache_seconds=int(os.environ.get("NARRATIVE_CACHE_SECONDS") or 3600),
    )


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(DEFAULT_STATIC_DIR), static_url_path="/static")
    app.config.update(
        DATA_DIR=Path(os.environ.get("LAKE_ATLAS_DATA_DIR") or DEFAULT_DATA_DIR),
        JSON_SORT_KEYS=False,
    )
    if config:
        app.config.update(config)

    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env", override=False)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        data_dir: Path = app.config["DATA_DIR"]
        settings = deepseek_settings()
        return jsonify({
            "ok": True,
            "csvPresent": (data_dir / CSV_FILE_NAME).is_file(),
            "geojsonPresent": (data_dir / GEOJSON_FILE_NAME).is_file(),
            "deepseekConfigured": settings.configured,
            "deepseekModel": settings.model,
        })

    @app.get("/api/lake-data")
    def lake_data():
        data_dir: Path = app.config["DATA_DIR"]
        csv_text, geojson, stats = load_and_summarize(data_dir)
        narratives, status, warnings = request_narratives(
            stats,
            deepseek_settings(),
            refresh=request.args.get("refresh") in {"1", "true", "yes"},
        )
        for warning in warnings:
            app.logger.warning(warning)

        return jsonify({
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "csv": csv_text,
            "csvFileName": CSV_FILE_NAME,
            "geojson": geojson,
            "geojsonFileName": GEOJSON_FILE_NAME,
            "stats": stats,
            "narratives": narratives,
            "narrativeStatus": status,
            "warnings": warnings,
            "source": {
                "csv": str((data_dir / CSV_FILE_NAME).relative_to(BASE_DIR))
                if (data_dir / CSV_FILE_NAME).is_relative_to(BASE_DIR)
                else str(data_dir / CSV_FILE_NAME),
                "geojson": str((data_dir / GEOJSON_FILE_NAME).relative_to(BASE_DIR))
                if (data_dir / GEOJSON_FILE_NAME).is_relative_to(BASE_DIR)
                else str(data_dir / GEOJSON_FILE_NAME),
            },
        })

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify({"error": error.message}), error.status

    @app.errorhandler(404)
    def handle_not_found(_error):
        return jsonify({"error": "Not found."}), 404

    @app.errorhandler(500)
    def handle_server_error(error):
        app.logger.exception("Unhandled server error", exc_info=error)
        return jsonify({"error": "The server could not complete this request."}), 500

    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("LAKE_ATLAS_HOST", "127.0.0.1")
    port = int(os.environ.get("LAKE_ATLAS_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
