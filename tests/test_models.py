"""Tests for privacy-preserving Equilab domain normalization."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from pyequilab import (
    Horse,
    Training,
    horse_from_data,
    number,
    rider_from_data,
    stable_from_data,
)


def training(identifier: str = "training", **changes):
    values = dict(
        id=identifier,
        start=datetime(2026, 9, 9, 12, tzinfo=UTC),
        kind="Dressage",
        duration=600.0,
        distance=1000.0,
        energy=2.0,
        gaits={"walk": 300.0},
    )
    values.update(changes)
    return Training(**values)


def horse(*items: Training, skipped: int = 0) -> Horse:
    return Horse("horse", "Horse", "Breed", None, "pony", "Level", True, items, skipped)


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, None),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (0, 0.0),
        (2.5, 2.5),
        ("3", None),
        (None, None),
    ],
)
def test_number(value, expected):
    assert number(value) == expected


def test_training_parses_supported_summary_fields():
    item = Training.parse(
        "training",
        {
            "user": "rider",
            "horse": "horse",
            "date": datetime(2026, 9, 9, tzinfo=UTC),
            "trainingTypes": {"-KgsjZPxgZP7Nsj_cXvW": True, "unknown": True},
            "total": {"time": 600, "distance": 1000, "topSpeed": 3},
            "riderEnergy": 0,
            "horseEnergyMj": 2,
            "walk": {
                "time": 300,
                "beat": 1.2,
                "pace": 1.5,
                "leftTurnsDuration": 30,
                "rightTurnsDuration": 20,
                "counter": 100,
                "stride": 2,
                "distance": 450,
            },
            "weather": {
                "temperature": -4,
                "apparentTemperature": -6,
                "humidity": 0.46,
                "windSpeed": 2.5,
                "pressure": 1013,
            },
            "transitionData": {"walk": {"trot": 2}, "trot": {"walk": 1}},
        },
    )
    assert item.kind == "Dressage, Other"
    assert item.duration == 600
    assert item.end == datetime(2026, 9, 9, 0, 10, tzinfo=UTC)
    assert item.gaits["walk"] == 300
    assert item.stats["walk_tempo"] == 72
    assert item.stats["walk_speed"] == 1.5
    assert item.stats["walk_left_rein"] == 30
    assert item.stats["weather_humidity"] == 46
    assert item.stats["weather_temperature"] == -4
    assert item.stats["transitions"] == 3
    assert item.rider_energy == 0
    assert item.horse_id == "horse" and item.rider_id == "rider"


def test_training_rejects_incomplete_or_invalid_values():
    assert Training.parse("id", {}).end is None
    assert Training.parse("id", {"date": datetime(2026, 1, 1)}).start is None
    invalid = Training.parse(
        "id",
        {
            "total": "invalid",
            "weather": {"humidity": 50, "temperature": True, "windSpeed": float("nan")},
            "transitionData": {"walk": {"trot": "invalid"}, "bad": "row"},
        },
    )
    assert invalid.stats["weather_humidity"] is None
    assert "weather_temperature" not in invalid.stats
    assert invalid.stats["transitions"] is None
    overflowing = training(start=datetime.max.replace(tzinfo=UTC), duration=600)
    assert overflowing.end is None


def test_profiles_discard_private_source_fields_and_normalize_records():
    data = {
        "name": "Horse",
        "birthDate": "2010-04-12",
        "note": "PRIVATE",
        "fcmTokens": {"SECRET": True},
        "records": [
            {"type": "distance", "value": 2000},
            {"type": "distance", "value": 1000},
            {"type": "duration", "value": 3600},
            {"type": "speed", "value": float("nan")},
        ],
    }
    item = horse_from_data("horse", data, True, (), 0)
    assert item.birthday == "2010-04-12"
    assert item.profile["record_distance"] == 2000
    assert item.profile["record_duration"] == 3600
    assert item.profile["record_speed"] is None
    assert "PRIVATE" not in repr(item)
    assert "SECRET" not in repr(item)
    assert horse_from_data("id", {"birthDate": "not-a-date"}, True, (), 0).birthday is None
    assert horse_from_data("id", {"birthDate": datetime(2012, 1, 2)}, True, (), 0).birthday == (
        "2012-01-02"
    )


def test_rider_and_stable_profiles():
    ride = training()
    notifications = [
        {"date": datetime(2026, 9, 9, tzinfo=UTC), "type": "older"},
        {"date": datetime(2026, 9, 10, tzinfo=UTC), "type": "new"},
        {"date": "invalid", "type": "ignored"},
    ]
    rider = rider_from_data(
        "rider",
        {
            "firstName": "Synthetic",
            "lastName": "Rider",
            "totalTrainingTime": 3600,
            "achievements": {"one": True},
            "records": [],
        },
        (ride,),
        0,
        notifications,
    )
    assert rider.name == "Synthetic Rider"
    assert rider.latest is ride
    assert rider.values["lastNotificationType"] == "new"
    assert rider.values["achievementsCount"] == 1
    assert rider.values["recordsCount"] == 0
    assert rider_from_data("id", {}, (), 0, []).name == "Rider"

    stable = stable_from_data(
        "stable",
        {"name": "Stable", "horses": {"a": True, "b": False}, "lat": 51.5, "lon": -1.5},
    )
    assert stable.values["horseCount"] == 1
    assert stable.values["lat"] == 51.5
    assert stable_from_data("s", {"lat": 91, "lon": -181}).values["lat"] is None
    assert stable_from_data("s", {}).name == "Stable or group"


def test_latest_and_calendar_period_aggregates():
    later = training("a")
    older = training("z", start=datetime(2026, 9, 8, tzinfo=UTC))
    assert horse(older, later).latest is later

    zone = ZoneInfo("Europe/London")
    now = datetime(2026, 9, 7, 1, tzinfo=zone)
    monday_local = training(start=datetime(2026, 9, 6, 23, 30, tzinfo=UTC))
    sunday_local = training(start=datetime(2026, 9, 6, 22, 30, tzinfo=UTC))
    future = training(start=datetime(2026, 9, 8, tzinfo=UTC))
    assert horse(monday_local, sunday_local, future).aggregate("week", "count", now) == 1
    assert horse(monday_local).aggregate("month", "distance", now) == 1000
    assert horse().aggregate("week", "energy", now) == 0
    assert (
        horse(training(start=monday_local.start, energy=None)).aggregate("week", "energy", now)
        is None
    )
    assert horse(training(), skipped=1).aggregate("week", "count", now) is None
    assert horse(training(start=None)).aggregate("week", "count", now) is None
