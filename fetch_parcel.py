#!/usr/bin/env python3
"""
fetch_parcel.py — retrieve a U.S. property boundary from a county ArcGIS
REST service and write a GeoJSON file ready for The Lake Atlas.

WHY THIS IS NOT A UNIVERSAL PARCEL API
--------------------------------------
There is no single national, authoritative parcel-boundary endpoint. Most
county GIS departments publish their parcels through ArcGIS Server as either
a MapServer or a FeatureServer. You need the county's service URL, for example:

    https://gis.examplecounty.gov/arcgis/rest/services/Parcels/MapServer
    https://gis.examplecounty.gov/arcgis/rest/services/Parcels/FeatureServer/0

Find it by searching for "<county name> GIS parcel REST" or by browsing the
county ArcGIS REST directory. The script can list the layers in a service and
auto-pick a parcel-like polygon layer. A direct layer URL is the most reliable.

QUICK START
-----------
    # Discover layers in a county service
    python fetch_parcel.py --service-url "https://.../MapServer" --list-layers

    # Address lookup (U.S. Census geocoder, no key required)
    python fetch_parcel.py \
        --address "123 Main St, Anytown, ME 04000" \
        --service-url "https://.../MapServer" \
        --out parcel.geojson

    # Coordinate lookup against a direct layer
    python fetch_parcel.py \
        --lat 44.3047 --lon -68.2438 \
        --service-url "https://.../FeatureServer/0" \
        --search-radius 30 --out parcel.geojson

    # Keep every parcel near the point instead of selecting one
    python fetch_parcel.py --lat ... --lon ... --all --service-url "..." --out nearby.geojson

ENVIRONMENT VARIABLES
---------------------
    ARCGIS_PARCEL_URL   Default service URL when --service-url is omitted.
    ARCGIS_TOKEN        Optional ArcGIS token for secured services.

TOKENS
------
Some public services allow anonymous access; others require a token from the
county (often the same token used by their ArcGIS web apps). Use --token or
ARCGIS_TOKEN. The token is sent as an ArcGIS query parameter and is redacted
from error output, but it may still appear in server/proxy logs. Prefer a short-
lived, least-privilege token, and never commit one to source control.

NOTES
-----
 * Output is EPSG:4326 (longitude, latitude), the format the HTML app expects.
 * The script preserves all ArcGIS attributes and adds a normalized `name`
   when it can infer a useful label from common address/parcel/owner fields.
 * Boundaries are planning data, not a legal survey. Check county disclaimers
   and recorded deeds before relying on a line.
 * Python 3.10+ and the standard library only.
"""

from __future__ import annotations
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

DEFAULT_TIMEOUT = 30.0
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = str(PROJECT_DIR / "data" / "parcel.geojson")
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
USER_AGENT = "TheLakeAtlas-ParcelFetcher/1.0 (local GIS utility; Python urllib)"

SERVICE_PATTERN = re.compile(
    r"^(?P<service>.*/(?:MapServer|FeatureServer))(?:/(?P<layer>\d+))?/?$",
    re.IGNORECASE,
)

PARCEL_TERMS = (
    "parcel", "parcels", "taxlot", "tax_lot", "tax lot", "cadastral",
    "property", "properties", "ownership", "land", "lot", "apn",
)
PENALTY_TERMS = (
    "annotation", "label", "dimension", "contour", "road", "street",
    "easement", "zoning", "hydrant", "pole", "centerline", "address_point",
    "address point", "trail", "tax district", "school", "voting", "table",
    # Retired/derived parcel layers are common in county services; prefer
    # the live cadastral layer when names are otherwise identical.
    "historical", "historic", "theoretical", "air rights",
)


class ParcelError(RuntimeError):
    """A predictable, user-facing failure."""


@dataclass
class LayerChoice:
    service_url: str
    layer_url: str
    layer_id: int
    name: str
    geometry_type: str
    info: dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact_url(url: str) -> str:
    """Remove token-like query values before printing or storing a URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in {"token", "access_token", "apikey", "api_key"}
        ]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
        )
    except ValueError:
        return "<invalid URL>"


def add_query(url: str, params: dict[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None or value == "":
            continue
        query[key] = str(value)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def normalize_endpoint(raw_url: str) -> tuple[str, Optional[int]]:
    """
    Return (service_root, direct_layer_id?). Accepts a MapServer/FeatureServer
    root or a direct layer URL, with optional query string and trailing slash.
    """
    if not raw_url or not raw_url.strip():
        raise ParcelError("A service URL is required. Use --service-url or set ARCGIS_PARCEL_URL.")
    raw_url = raw_url.strip()
    parts = urllib.parse.urlsplit(raw_url)
    if parts.scheme not in {"http", "https"}:
        raise ParcelError("The service URL must start with http:// or https://.")
    path = re.sub(r"/(?:query|legend|export|identify)/?$", "", parts.path, flags=re.IGNORECASE)
    cleaned = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))
    match = SERVICE_PATTERN.match(cleaned)
    if not match:
        raise ParcelError(
            "That does not look like an ArcGIS MapServer or FeatureServer URL.\n"
            "Expected something ending in /MapServer, /FeatureServer, or /MapServer/<layer>.\n"
            "Browse the county's ArcGIS REST directory to find the parcel service."
        )
    service_url = match.group("service").rstrip("/")
    direct_layer = int(match.group("layer")) if match.group("layer") is not None else None
    return service_url, direct_layer


def http_json(
    url: str,
    *,
    token: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """GET JSON from an ArcGIS/REST endpoint and normalize service errors."""
    query: dict[str, Any] = {"f": "json"}
    if params:
        query.update(params)
    if token:
        query["token"] = token
    full_url = add_query(url, query)
    request = urllib.request.Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(2.0, float(timeout))) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            pass
        detail = f" HTTP {exc.code}."
        if exc.code in {401, 403}:
            detail += " The service may require an ArcGIS token; try --token."
        if body and not body.lstrip().startswith("<"):
            detail += f" Response: {body.strip()[:400]}"
        raise ParcelError(f"Request failed for {redact_url(full_url)}.{detail}") from exc
    except urllib.error.URLError as exc:
        raise ParcelError(
            f"Could not reach {redact_url(full_url)}. Check the network, VPN, "
            f"or service host. ({exc.reason})"
        ) from exc
    except TimeoutError as exc:
        raise ParcelError(
            f"The service timed out after {timeout:g}s. Try again or raise --timeout."
        ) from exc

    try:
        data = json.loads(text.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        snippet = re.sub(r"\s+", " ", text[:300]).strip()
        raise ParcelError(
            f"The endpoint did not return JSON. Check the service URL. Response began: {snippet!r}"
        ) from exc

    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message", "ArcGIS service error")
        details = error.get("details") or []
        detail_text = " ".join(str(item) for item in details if item)
        suffix = f" {detail_text}" if detail_text else ""
        hint = ""
        if error.get("code") in {498, 499, 403}:
            hint = " The token may be missing, expired, or lack access to this layer."
        raise ParcelError(f"ArcGIS error {error.get('code', '')}: {message}.{suffix}{hint}".strip())
    if not isinstance(data, dict):
        raise ParcelError("The endpoint returned JSON in an unexpected shape.")
    return data


def fetch_layer_info(layer_url: str, token: Optional[str], timeout: float) -> dict[str, Any]:
    return http_json(layer_url, token=token, timeout=timeout)


def score_layer(layer: dict[str, Any]) -> int:
    name = str(layer.get("name", "")).lower()
    geometry = str(layer.get("geometryType", ""))
    score = 0
    if geometry.lower() == "esrigeometrypolygon":
        score += 12
    if "parcel" in name:
        score += 24
    for term in PARCEL_TERMS:
        if term in name:
            score += 7
    for term in PENALTY_TERMS:
        if term in name:
            score -= 22
    if str(layer.get("type", "")).lower() == "group layer":
        score -= 30
    return score


def choose_layer(
    service_url: str,
    direct_layer: Optional[int],
    selector: Optional[str],
    token: Optional[str],
    timeout: float,
) -> LayerChoice:
    if direct_layer is not None:
        layer_url = f"{service_url}/{direct_layer}"
        info = fetch_layer_info(layer_url, token, timeout)
        name = str(info.get("name") or f"Layer {direct_layer}")
        geometry = str(info.get("geometryType") or "")
        if geometry and geometry.lower() != "esrigeometrypolygon":
            raise ParcelError(
                f"Layer {direct_layer} ({name}) is '{geometry}', not a polygon layer. "
                "Choose a parcel polygon layer."
            )
        return LayerChoice(service_url, layer_url, direct_layer, name, geometry, info)

    service_info = http_json(service_url, token=token, timeout=timeout)
    layers = [
        layer for layer in (service_info.get("layers") or [])
        if isinstance(layer, dict) and layer.get("id") is not None
    ]
    if not layers:
        raise ParcelError(
            "The service metadata lists no feature layers. Confirm the URL, or pass a "
            "direct layer URL ending in /MapServer/<id> or /FeatureServer/<id>."
        )

    chosen: Optional[dict[str, Any]] = None
    if selector:
        selector = selector.strip()
        if selector.isdigit():
            wanted_id = int(selector)
            chosen = next((layer for layer in layers if int(layer["id"]) == wanted_id), None)
            if chosen is None:
                available = ", ".join(f"{layer['id']}: {layer.get('name', 'unnamed')}" for layer in layers)
                raise ParcelError(f"No layer with id {wanted_id}. Available: {available}")
        else:
            try:
                matcher = re.compile(selector, re.IGNORECASE)
            except re.error as exc:
                raise ParcelError(f"--layer is not a valid regular expression: {exc}") from exc
            matches = [layer for layer in layers if matcher.search(str(layer.get("name", "")))]
            if not matches:
                available = ", ".join(f"{layer['id']}: {layer.get('name', 'unnamed')}" for layer in layers)
                raise ParcelError(f"No layer name matched {selector!r}. Available: {available}")
            if len(matches) > 1:
                available = ", ".join(f"{layer['id']}: {layer.get('name', 'unnamed')}" for layer in matches)
                raise ParcelError(f"Multiple layers matched {selector!r}: {available}. Use --layer <id>.")
            chosen = matches[0]
    else:
        ranked = sorted(layers, key=score_layer, reverse=True)
        if ranked and score_layer(ranked[0]) > 0:
            if len(ranked) > 1 and score_layer(ranked[0]) == score_layer(ranked[1]):
                available = ", ".join(f"{layer['id']}: {layer.get('name', 'unnamed')}" for layer in layers)
                raise ParcelError(
                    "Could not choose between equally plausible parcel layers. "
                    f"Pass --layer <id> or --layer <name regex>. Available: {available}"
                )
            chosen = ranked[0]
        else:
            polygon_layers = [
                layer for layer in layers
                if str(layer.get("geometryType", "")).lower() == "esrigeometrypolygon"
            ]
            if len(polygon_layers) == 1:
                chosen = polygon_layers[0]
            else:
                available = ", ".join(f"{layer['id']}: {layer.get('name', 'unnamed')}" for layer in layers)
                raise ParcelError(
                    "No parcel-like layer was obvious. "
                    f"Pass --layer <id> or --layer <name regex>. Available: {available}"
                )

    if chosen is None:
        raise ParcelError("Could not select a layer. Use --layer to choose one explicitly.")

    layer_id = int(chosen["id"])
    layer_url = f"{service_url}/{layer_id}"
    info = fetch_layer_info(layer_url, token, timeout)
    geometry = str(info.get("geometryType") or chosen.get("geometryType") or "")
    if geometry and geometry.lower() != "esrigeometrypolygon":
        raise ParcelError(
            f"Layer {layer_id} ({info.get('name', chosen.get('name', ''))}) is {geometry}, "
            "not a polygon layer. Use --layer to select a parcel polygon layer."
        )
    return LayerChoice(
        service_url=service_url,
        layer_url=layer_url,
        layer_id=layer_id,
        name=str(info.get("name") or chosen.get("name") or f"Layer {layer_id}"),
        geometry_type=geometry,
        info=info,
    )


def list_layers(service_url: str, direct_layer: Optional[int], token: Optional[str], timeout: float) -> None:
    if direct_layer is not None:
        info = fetch_layer_info(f"{service_url}/{direct_layer}", token, timeout)
        print(f"{direct_layer}: {info.get('name', 'unnamed')} [{info.get('geometryType', 'unknown geometry')}]")
        return
    data = http_json(service_url, token=token, timeout=timeout)
    layers = [layer for layer in (data.get("layers") or []) if isinstance(layer, dict)]
    if not layers:
        print("No layers were listed by this service.", file=sys.stderr)
        return
    print(f"Service: {data.get('mapName') or data.get('name') or service_url}")
    print(f"URL: {service_url}")
    for layer in layers:
        print(f"{layer.get('id', '?'):>4}: {layer.get('name', 'unnamed')} "
              f"[{layer.get('geometryType', 'unknown geometry')}]")
    tables = data.get("tables") or []
    if tables:
        print("\nTables:")
        for table in tables:
            if isinstance(table, dict):
                print(f"{table.get('id', '?'):>4}: {table.get('name', 'unnamed')} [table]")


def geocode_address(address: str, timeout: float) -> dict[str, Any]:
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    data = http_json(CENSUS_GEOCODER, timeout=timeout, params=params)
    matches = (((data.get("result") or {}).get("addressMatches")) or [])
    if not matches:
        raise ParcelError(
            "The U.S. Census geocoder found no match for that address. "
            "Try a fuller street address, or supply --lat and --lon directly."
        )
    match = matches[0]
    coordinates = match.get("coordinates") or {}
    lon = coordinates.get("x")
    lat = coordinates.get("y")
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        raise ParcelError("The geocoder returned a match without usable coordinates.")
    return {
        "matched_address": match.get("matchedAddress") or address,
        "lat": float(lat),
        "lon": float(lon),
        "source": "U.S. Census Geocoder (Public_AR_Current)",
        "query_address": address,
    }


def validate_point(lat: float, lon: float) -> tuple[float, float]:
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ParcelError("Latitude and longitude must be finite numbers.")
    if not -90 <= lat <= 90:
        raise ParcelError(f"Latitude {lat} is outside [-90, 90].")
    if not -180 <= lon <= 180:
        raise ParcelError(f"Longitude {lon} is outside [-180, 180].")
    return float(lat), float(lon)


def search_envelope(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ParcelError("--search-radius must be a positive number of metres.")
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    return (
        max(-180.0, lon - lon_delta),
        max(-90.0, lat - lat_delta),
        min(180.0, lon + lon_delta),
        min(90.0, lat + lat_delta),
    )


def query_parcels(
    layer: LayerChoice,
    *,
    lat: float,
    lon: float,
    radius_m: float,
    where: str,
    token: Optional[str],
    timeout: float,
) -> tuple[list[dict[str, Any]], str]:
    xmin, ymin, xmax, ymax = search_envelope(lat, lon, radius_m)
    query_url = f"{layer.layer_url}/query"
    params = {
        "where": where or "1=1",
        "geometry": f"{xmin:.8f},{ymin:.8f},{xmax:.8f},{ymax:.8f}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "true",
        "returnTrueCurves": "false",
        "returnZ": "false",
        "returnM": "false",
        "outFields": "*",
        "resultRecordCount": "100",
        "returnExceededLimitFeatures": "true",
        "geometryPrecision": "7",
    }
    data = http_json(query_url, token=token, timeout=timeout, params=params)
    features = data.get("features")
    if features is None:
        features = []
    if not isinstance(features, list):
        raise ParcelError("The query response did not contain a feature array.")
    if data.get("exceededTransferLimit"):
        print(
            "warning: the service reported an exceeded transfer limit; only the first "
            "100 intersecting parcels were considered.",
            file=sys.stderr,
        )
    return features, query_url


def signed_area(ring: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index + 1][0], ring[index + 1][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def orient_ring(ring: list[list[float]], counter_clockwise: bool) -> list[list[float]]:
    currently_ccw = signed_area(ring) > 0
    if currently_ccw != counter_clockwise:
        return list(reversed(ring))
    return ring


def point_in_ring(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting; boundary behavior is unspecified but stable enough here."""
    inside = False
    count = len(ring)
    if count < 4:
        return False
    for index in range(count - 1):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index + 1][0], ring[index + 1][1]
        crosses = (y1 > y) != (y2 > y)
        if not crosses:
            continue
        denominator = y2 - y1
        if denominator == 0:
            continue
        intersection_x = x1 + (y - y1) * (x2 - x1) / denominator
        if x < intersection_x:
            inside = not inside
    return inside


def clean_ring(raw_ring: Any) -> Optional[list[list[float]]]:
    """Drop Z/M values, remove repeated vertices, and close the ring."""
    if not isinstance(raw_ring, (list, tuple)):
        return None
    coordinates: list[list[float]] = []
    for raw_point in raw_ring:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 2:
            continue
        try:
            x = float(raw_point[0])
            y = float(raw_point[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        if not coordinates or coordinates[-1] != [x, y]:
            coordinates.append([x, y])
    if len(coordinates) >= 2 and coordinates[0] == coordinates[-1]:
        coordinates.pop()
    if len(coordinates) < 3:
        return None
    coordinates.append(coordinates[0][:])
    if abs(signed_area(coordinates)) <= 1e-14:
        return None
    return coordinates


def esri_rings_to_geojson(rings: Iterable[Any]) -> Optional[dict[str, Any]]:
    """
    Convert Esri polygon rings to GeoJSON Polygon/MultiPolygon.

    ArcGIS does not guarantee a simple outer-then-hole order in every county
    export, so we use containment depth rather than ring order:
      - a ring contained by an even number of larger rings is an outer ring
      - an odd count makes it a hole in its immediate even-depth parent
    This also preserves islands enclosed by a hole as separate polygons.
    """
    cleaned = []
    for raw_ring in rings or []:
        coordinates = clean_ring(raw_ring)
        if coordinates:
            area = abs(signed_area(coordinates))
            if area > 0:
                cleaned.append({"coordinates": coordinates, "area": area})

    if not cleaned:
        return None
    cleaned.sort(key=lambda ring: ring["area"], reverse=True)

    for index, ring in enumerate(cleaned):
        ring["depth"] = 0
        ring["parent"] = None
        representative = ring["coordinates"][0]
        containing = []
        for other_index in range(index):
            other = cleaned[other_index]
            if point_in_ring(representative[0], representative[1], other["coordinates"]):
                containing.append(other_index)
        ring["depth"] = len(containing)
        if containing:
            ring["parent"] = min(containing, key=lambda item: cleaned[item]["area"])

    outer_rings = []
    outer_indices: dict[int, int] = {}
    for index, ring in enumerate(cleaned):
        if ring["depth"] % 2 == 0:
            ring["coordinates"] = orient_ring(ring["coordinates"], counter_clockwise=True)
            outer_indices[index] = len(outer_rings)
            outer_rings.append([ring["coordinates"]])

    if not outer_rings:
        return None

    for index, ring in enumerate(cleaned):
        if ring["depth"] % 2 == 0:
            continue
        # Find the nearest containing ring that is itself an outer polygon.
        candidate_parents = []
        representative = ring["coordinates"][0]
        for other_index, other in enumerate(cleaned[:index]):
            if other["depth"] % 2 == 0 and point_in_ring(
                representative[0], representative[1], other["coordinates"]
            ):
                candidate_parents.append(other_index)
        if not candidate_parents:
            continue
        parent_index = min(candidate_parents, key=lambda item: cleaned[item]["area"])
        hole = orient_ring(ring["coordinates"], counter_clockwise=False)
        target = outer_indices.get(parent_index)
        if target is not None:
            outer_rings[target].append(hole)

    if len(outer_rings) == 1:
        return {"type": "Polygon", "coordinates": outer_rings[0]}
    return {"type": "MultiPolygon", "coordinates": outer_rings}


def geometry_contains(geometry: dict[str, Any], lon: float, lat: float) -> bool:
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif geometry_type == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        return False

    for polygon in polygons:
        if not polygon:
            continue
        outer = polygon[0]
        if not point_in_ring(lon, lat, outer):
            continue
        inside_hole = any(point_in_ring(lon, lat, hole) for hole in polygon[1:])
        if not inside_hole:
            return True
    return False


def approximate_area_m2(geometry: dict[str, Any]) -> float:
    """
    Fast planar approximation in metres for ranking candidates. This is not a
    geodetic area and should not be treated as a legal or survey measurement.
    """
    polygons = (
        [geometry.get("coordinates") or []]
        if geometry.get("type") == "Polygon"
        else (geometry.get("coordinates") or [])
    )
    if not polygons:
        return 0.0
    all_latitudes = [
        point[1]
        for polygon in polygons
        for ring in polygon
        for point in ring
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not all_latitudes:
        return 0.0
    mean_lat = sum(all_latitudes) / len(all_latitudes)
    x_scale = 111_320.0 * max(0.01, math.cos(math.radians(mean_lat)))
    y_scale = 110_574.0

    def projected_area(ring: Sequence[Sequence[float]]) -> float:
        meters = [(point[0] * x_scale, point[1] * y_scale) for point in ring]
        total = 0.0
        for index in range(len(meters) - 1):
            x1, y1 = meters[index]
            x2, y2 = meters[index + 1]
            total += x1 * y2 - x2 * y1
        return abs(total / 2.0)

    total_area = 0.0
    for polygon in polygons:
        if not polygon:
            continue
        total_area += projected_area(polygon[0])
        total_area -= sum(projected_area(hole) for hole in polygon[1:])
    return max(0.0, total_area)


def geometry_centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    points = []
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polygons = [coordinates]
    elif geometry.get("type") == "MultiPolygon":
        polygons = coordinates
    else:
        return (0.0, 0.0)
    for polygon in polygons:
        if polygon:
            points.extend(polygon[0])
    if not points:
        return (0.0, 0.0)
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


LABEL_FIELDS = (
    "name", "parcel_name", "site_name", "property_name", "situs_address",
    "situs", "site_address", "property_address", "address", "address_1",
    "sitadd", "parcel_id", "parcelid", "parcel_number", "apn", "pin",
    "owner_name", "ownername", "owner", "label",
)


def attribute_lookup(attributes: dict[str, Any], wanted: str) -> Optional[Any]:
    target = re.sub(r"[^a-z0-9]+", "_", str(wanted).lower()).strip("_")
    for key, value in attributes.items():
        normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
        if normalized == target and value not in (None, ""):
            return value
    return None


def label_from_attributes(attributes: dict[str, Any]) -> Optional[str]:
    for field in LABEL_FIELDS:
        value = attribute_lookup(attributes, field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def convert_esri_feature(feature: dict[str, Any]) -> Optional[dict[str, Any]]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or not geometry.get("rings"):
        return None
    spatial_ref = geometry.get("spatialReference")
    if isinstance(spatial_ref, dict):
        wkid = spatial_ref.get("latestWkid", spatial_ref.get("wkid"))
        if wkid is not None:
            try:
                wkid_int = int(wkid)
            except (TypeError, ValueError):
                wkid_int = None
            if wkid_int is not None and wkid_int != 4326:
                raise ParcelError(
                    f"The service returned coordinates in EPSG:{wkid_int}, despite outSR=4326. "
                    "This endpoint does not honor WGS84 output; use a layer/service that does, "
                    "or project the result to EPSG:4326 before loading it into the atlas."
                )
    converted_geometry = esri_rings_to_geojson(geometry.get("rings") or [])
    if not converted_geometry:
        return None
    attributes = feature.get("attributes") if isinstance(feature.get("attributes"), dict) else {}
    properties = dict(attributes)
    # A normalized label makes the file display nicely in the HTML app while
    # leaving every original county attribute intact.
    if "name" not in properties or not str(properties.get("name") or "").strip():
        label = label_from_attributes(attributes)
        if label:
            properties["name"] = label
    return {
        "type": "Feature",
        "geometry": converted_geometry,
        "properties": properties,
    }


def select_features(
    features: list[dict[str, Any]],
    *,
    lat: float,
    lon: float,
    take_all: bool,
) -> tuple[list[dict[str, Any]], str]:
    converted = []
    for feature in features:
        geo = convert_esri_feature(feature)
        if geo:
            geo_area = approximate_area_m2(geo["geometry"])
            converted.append({"feature": geo, "area": geo_area})
    if not converted:
        raise ParcelError(
            "The query found features but none contained usable polygon geometry. "
            "Confirm that the selected layer is a parcel polygon layer."
        )

    if take_all:
        return [item["feature"] for item in converted], "all intersecting parcels"

    containing = [
        item for item in converted
        if geometry_contains(item["feature"]["geometry"], lon, lat)
    ]
    if containing:
        selected = min(containing, key=lambda item: item["area"])
        return [selected["feature"]], "parcel containing the query point"

    def metres_from_query(item: dict[str, Any]) -> float:
        centroid_lon, centroid_lat = geometry_centroid(item["feature"]["geometry"])
        x = (centroid_lon - lon) * 111_320.0 * max(0.01, math.cos(math.radians(lat)))
        y = (centroid_lat - lat) * 110_574.0
        return math.hypot(x, y)

    selected = min(converted, key=metres_from_query)
    return [selected["feature"]], "nearest intersecting parcel (the point fell outside all returned polygons)"


def output_summary(
    *,
    layer: LayerChoice,
    location_label: str,
    selected: list[dict[str, Any]],
    candidates: int,
    selection: str,
    output_path: str,
) -> None:
    vertex_count = 0
    for feature in selected:
        geometry = feature.get("geometry") or {}
        polygons = (
            [geometry.get("coordinates") or []]
            if geometry.get("type") == "Polygon"
            else (geometry.get("coordinates") or [])
        )
        for polygon in polygons:
            for ring in polygon:
                vertex_count += len(ring)
    approx_acres = sum(approximate_area_m2(feature["geometry"]) for feature in selected) / 4046.8564224
    print("")
    print(f"Fetched {len(selected)} parcel feature{'s' if len(selected) != 1 else ''} from:")
    print(f"  {layer.name} — layer {layer.layer_id}")
    print(f"  {layer.service_url}")
    print(f"Location: {location_label}")
    print(f"Selection: {selection} ({candidates} candidate{'s' if candidates != 1 else ''} returned)")
    print(f"Geometry: {vertex_count} vertices · approx. {approx_acres:,.2f} acres")
    print(f"Output: {output_path}")
    print("Verify boundaries and disclaimers with the county before relying on them.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a U.S. parcel boundary from a county ArcGIS REST service and export "
            "one or more polygons as EPSG:4326 GeoJSON for The Lake Atlas."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_parcel.py --service-url https://gis.co.gov/arcgis/rest/services/Parcels/MapServer --list-layers\n"
            "  python fetch_parcel.py --address \"123 Main St, Town, ME 04000\" --service-url https://.../MapServer\n"
            "  python fetch_parcel.py --lat 44.3047 --lon -68.2438 --service-url https://.../FeatureServer/0\n\n"
            "Environment:\n"
            "  ARCGIS_PARCEL_URL   Default service URL\n"
            "  ARCGIS_TOKEN        Token for secured county services\n"
        ),
    )
    parser.add_argument("--address", help="U.S. street address to geocode with the Census geocoder.")
    parser.add_argument("--lat", type=float, help="Latitude (WGS84) of the location.")
    parser.add_argument("--lon", type=float, help="Longitude (WGS84) of the location.")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("ARCGIS_PARCEL_URL"),
        help="ArcGIS MapServer/FeatureServer root, or a direct layer URL. Default: ARCGIS_PARCEL_URL.",
    )
    parser.add_argument(
        "--layer",
        help="Layer id, or a case-insensitive regular expression matched against layer names. "
             "Defaults to auto-detecting a parcel polygon layer.",
    )
    parser.add_argument(
        "--where",
        default="1=1",
        help="Optional ArcGIS SQL WHERE clause in addition to the spatial query. Default: 1=1.",
    )
    parser.add_argument(
        "--search-radius",
        type=float,
        default=25.0,
        help="Spatial search radius around the point, in metres. Default: 25.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ARCGIS_TOKEN"),
        help="ArcGIS token for a secured service. Default: ARCGIS_TOKEN.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Export all intersecting parcels instead of selecting one.",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help='Output GeoJSON path. Use "-" for stdout. Default: data/parcel.geojson.',
    )
    parser.add_argument(
        "--list-layers",
        action="store_true",
        help="List the layers in the service and exit. Coordinates are not needed.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Network timeout in seconds. Default: {DEFAULT_TIMEOUT:g}.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if not args.service_url:
            raise ParcelError(
                "No service URL supplied. Use --service-url, or set ARCGIS_PARCEL_URL.\n"
                "Find your county's parcel service at its ArcGIS REST directory; this script "
                "cannot guess one national endpoint."
            )

        # A token can be supplied in the URL as well as via --token; strip the
        # query from the endpoint either way so it is never written to output.
        url_query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(args.service_url).query))
        token = args.token or url_query.get("token") or url_query.get("access_token") or None
        service_url, direct_layer = normalize_endpoint(args.service_url)

        if args.list_layers:
            list_layers(service_url, direct_layer, token, args.timeout)
            return 0

        if args.address and (args.lat is not None or args.lon is not None):
            raise ParcelError("Use either --address or --lat/--lon, not both.")
        if args.lat is None and args.lon is None and not args.address:
            raise ParcelError("Provide a location with --address, or with both --lat and --lon.")
        if (args.lat is None) != (args.lon is None):
            raise ParcelError("--lat and --lon must be supplied together.")

        geocoder: Optional[dict[str, Any]] = None
        if args.address:
            if not args.address.strip():
                raise ParcelError("--address cannot be empty.")
            geocoder = geocode_address(args.address.strip(), args.timeout)
            lat, lon = validate_point(geocoder["lat"], geocoder["lon"])
            location_label = f"{geocoder['matched_address']} ({lat:.6f}, {lon:.6f})"
        else:
            lat, lon = validate_point(args.lat, args.lon)  # type: ignore[arg-type]
            location_label = f"{lat:.6f}, {lon:.6f}"

        layer = choose_layer(service_url, direct_layer, args.layer, token, args.timeout)
        features, query_url = query_parcels(
            layer,
            lat=lat,
            lon=lon,
            radius_m=args.search_radius,
            where=args.where,
            token=token,
            timeout=args.timeout,
        )
        if not features:
            raise ParcelError(
                f"No parcel polygons intersected the search envelope on layer "
                f"{layer.layer_id} ({layer.name}). Try a larger --search-radius, "
                "confirm the layer/where clause, compare another layer with "
                "--list-layers/--layer, or verify the point is inside the "
                "service's extent."
            )

        selected, selection = select_features(
            features,
            lat=lat,
            lon=lon,
            take_all=bool(args.all),
        )

        collection_name = label_from_attributes(selected[0].get("properties") or {})
        collection = {
            "type": "FeatureCollection",
            "name": collection_name or "Parcel boundary",
            "metadata": {
                "generator": "fetch_parcel.py",
                "generated_utc": utc_now_iso(),
                "source": {
                    "service_url": layer.service_url,
                    "layer_id": layer.layer_id,
                    "layer_name": layer.name,
                    "query_url": redact_url(query_url),
                },
                "query": {
                    "point": {"lat": lat, "lon": lon},
                    "search_radius_m": args.search_radius,
                    "where": args.where,
                    "spatial_relation": "intersects",
                    "input_crs": "EPSG:4326",
                    "output_crs": "EPSG:4326",
                },
                "geocoder": geocoder,
                "selection": selection,
                "candidate_count": len(features),
                "selected_count": len(selected),
                "disclaimer": (
                    "Parcel boundaries are planning data, not a legal survey. "
                    "Verify against recorded county records before use."
                ),
            },
            "features": selected,
        }

        payload = json.dumps(collection, ensure_ascii=False, indent=2) + "\n"
        if args.out == "-":
            sys.stdout.write(payload)
            print("", file=sys.stderr)
            output_summary(
                layer=layer,
                location_label=location_label,
                selected=selected,
                candidates=len(features),
                selection=selection,
                output_path="stdout",
            )
        else:
            output_path = Path(args.out).expanduser()
            if not output_path.is_absolute():
                output_path = Path.cwd() / output_path
            if output_path.parent and not output_path.parent.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8")
            output_summary(
                layer=layer,
                location_label=location_label,
                selected=selected,
                candidates=len(features),
                selection=selection,
                output_path=str(output_path),
            )
        return 0
    except ParcelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
