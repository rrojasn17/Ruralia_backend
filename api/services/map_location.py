from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx


_COORDINATE_PAIR = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_GOOGLE_DATA_PAIR = re.compile(r"!3d(-?\d{1,3}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)")
_GOOGLE_AT_PAIR = re.compile(r"@(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 6
_MAX_HTML_BYTES = 512_000
_ALLOWED_HOSTS = {
    "google.com",
    "www.google.com",
    "maps.google.com",
    "google.co.cr",
    "www.google.co.cr",
    "maps.google.co.cr",
    "maps.app.goo.gl",
    "goo.gl",
}


class MapLocationError(ValueError):
    """Error seguro y presentable al resolver una ubicación cartográfica."""


@dataclass(frozen=True)
class ResolvedMapLocation:
    latitude: float
    longitude: float
    canonical_url: str
    source: str


def _valid_coordinates(latitude: float, longitude: float) -> tuple[float, float] | None:
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


def _coordinates_from_match(match: re.Match[str] | None) -> tuple[float, float] | None:
    if match is None:
        return None
    return _valid_coordinates(float(match.group(1)), float(match.group(2)))


def _decode_map_value(value: str) -> str:
    decoded = value
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return unescape(decoded).replace(r"\u0026", "&").replace(r"\u003d", "=")


def extract_map_coordinates(value: str) -> tuple[float, float] | None:
    """Extrae coordenadas de URLs comunes de Google Maps o de un par lat,lng."""

    raw_value = (value or "").strip()
    if not raw_value:
        return None

    decoded_value = _decode_map_value(raw_value)

    data_matches = list(_GOOGLE_DATA_PAIR.finditer(decoded_value))
    for match in reversed(data_matches):
        coordinates = _coordinates_from_match(match)
        if coordinates:
            return coordinates

    at_coordinates = _coordinates_from_match(_GOOGLE_AT_PAIR.search(decoded_value))
    if at_coordinates:
        return at_coordinates

    try:
        query = parse_qs(urlsplit(raw_value).query)
    except ValueError:
        query = {}

    for key in ("q", "query", "ll", "destination", "daddr"):
        for query_value in query.get(key, []):
            coordinates = _coordinates_from_match(
                _COORDINATE_PAIR.search(query_value.strip().removeprefix("loc:"))
            )
            if coordinates:
                return coordinates

    if not raw_value.lower().startswith(("http://", "https://")):
        plain_match = re.fullmatch(
            r"\s*@?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*",
            decoded_value,
        )
        return _coordinates_from_match(plain_match)

    return None


def _format_coordinate(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".")


def build_canonical_google_maps_url(latitude: float, longitude: float) -> str:
    return (
        "https://www.google.com/maps/search/?api=1&query="
        f"{_format_coordinate(latitude)}%2C{_format_coordinate(longitude)}"
    )


def _validate_google_maps_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MapLocationError("El enlace de Google Maps no es válido") from exc

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or hostname not in _ALLOWED_HOSTS:
        raise MapLocationError("Solo se permiten enlaces oficiales de Google Maps")
    if parsed.username or parsed.password:
        raise MapLocationError("El enlace de Google Maps no es válido")
    if port not in {None, 80, 443}:
        raise MapLocationError("El enlace de Google Maps usa un puerto no permitido")
    if hostname == "goo.gl" and not parsed.path.startswith("/maps/"):
        raise MapLocationError("El enlace corto no corresponde a Google Maps")

    return parsed.geturl()


def _resolved_result(coordinates: tuple[float, float], source: str) -> ResolvedMapLocation:
    latitude, longitude = coordinates
    return ResolvedMapLocation(
        latitude=latitude,
        longitude=longitude,
        canonical_url=build_canonical_google_maps_url(latitude, longitude),
        source=source,
    )


def resolve_map_location(value: str) -> ResolvedMapLocation:
    """Resuelve enlaces cortos sin aceptar destinos externos arbitrarios (SSRF)."""

    raw_value = (value or "").strip()
    direct_coordinates = extract_map_coordinates(raw_value)
    if direct_coordinates:
        return _resolved_result(direct_coordinates, "input")

    current_url = _validate_google_maps_url(raw_value)

    try:
        timeout = httpx.Timeout(8.0, connect=4.0)
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "NAVIA/1.0 map-location-resolver"},
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                coordinates = extract_map_coordinates(current_url)
                if coordinates:
                    return _resolved_result(coordinates, "redirect")

                with client.stream("GET", current_url) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise MapLocationError("Google Maps devolvió una redirección incompleta")
                        next_url = urljoin(current_url, location)
                        coordinates = extract_map_coordinates(next_url)
                        if coordinates:
                            return _resolved_result(coordinates, "redirect")
                        current_url = _validate_google_maps_url(next_url)
                        continue

                    if response.status_code >= 400:
                        raise MapLocationError("Google Maps no permitió consultar ese enlace")

                    content = bytearray()
                    for chunk in response.iter_bytes():
                        remaining = _MAX_HTML_BYTES - len(content)
                        if remaining <= 0:
                            break
                        content.extend(chunk[:remaining])

                    html_text = content.decode(response.encoding or "utf-8", errors="ignore")
                    coordinates = extract_map_coordinates(html_text)
                    if coordinates:
                        return _resolved_result(coordinates, "page")
                    break
    except httpx.HTTPError as exc:
        raise MapLocationError("No se pudo conectar con Google Maps para resolver el enlace") from exc

    raise MapLocationError(
        "No se encontraron coordenadas en el enlace. Abra el punto en Google Maps y copie un enlace con marcador."
    )
