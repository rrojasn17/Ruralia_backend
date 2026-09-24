from __future__ import annotations

import pytest

from services.map_location import (
    MapLocationError,
    build_canonical_google_maps_url,
    extract_map_coordinates,
    resolve_map_location,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9.8123456,-83.9123456", (9.8123456, -83.9123456)),
        (
            "https://www.google.com/maps/place/Finca/@9.8123,-83.9123,18z/"
            "data=!4m5!3m4!1sabc!8m2!3d9.8123456!4d-83.9123456",
            (9.8123456, -83.9123456),
        ),
        (
            "https://www.google.com/maps/search/?api=1&query=9.8123456%2C-83.9123456",
            (9.8123456, -83.9123456),
        ),
        (
            "https://maps.google.com/maps?ll=9.8123456,-83.9123456",
            (9.8123456, -83.9123456),
        ),
    ],
)
def test_extract_map_coordinates(value: str, expected: tuple[float, float]) -> None:
    assert extract_map_coordinates(value) == expected


def test_google_place_coordinates_take_priority_over_viewport() -> None:
    value = (
        "https://www.google.com/maps/place/Finca/@9.8,-83.9,14z/"
        "data=!4m5!3m4!1sabc!8m2!3d9.8123456!4d-83.9123456"
    )

    assert extract_map_coordinates(value) == (9.8123456, -83.9123456)


def test_resolve_direct_coordinates_without_network() -> None:
    location = resolve_map_location("9.8123456,-83.9123456")

    assert location.latitude == 9.8123456
    assert location.longitude == -83.9123456
    assert location.source == "input"
    assert location.canonical_url == build_canonical_google_maps_url(9.8123456, -83.9123456)


def test_resolver_rejects_non_google_hosts() -> None:
    with pytest.raises(MapLocationError, match="Solo se permiten"):
        resolve_map_location("http://127.0.0.1:8027/internal")


def test_invalid_coordinate_ranges_are_not_accepted() -> None:
    assert extract_map_coordinates("95.1,-83.9") is None
    assert extract_map_coordinates("9.8,-190.2") is None
