"""Normalized, deliberately minimal models; no raw profiles or notes retained."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast

from .api import active_ids

TYPE_NAMES: dict[str, str] = {
    "-KgsjZPxgZP7Nsj_cXvV": "Show jumping",
    "-KgsjZPxgZP7Nsj_cXvW": "Dressage",
    "-KgsjZPyo4n1dAYIvVQe": "Flatwork",
    "-KgsjZPyo4n1dAYIvVQf": "Hacking",
    "-KgsjZPyo4n1dAYIvVQg": "Trotting training",
    "-KgsjZPyo4n1dAYIvVQh": "Gallop training",
    "-KgsjZPyo4n1dAYIvVQi": "Cardio",
    "-KgsjZPzi754NLdGfZ4p": "Lungeing",
    "-KgsjZPzi754NLdGfZ4q": "Long reining",
    "-KgsjZPzi754NLdGfZ4r": "Walk training",
    "-KgsjZPzi754NLdGfZ4s": "Competition",
    "-KgsjZPzi754NLdGfZ4t": "Poles",
    "-KgsjZPzi754NLdGfZ4u": "Eventing",
    "-KgsjZPzi754NLdGfZ4v": "Strength",
    "-KgsjZPzi754NLdGfZ4y": "Driving",
    "-KgsjZPzi754NLdGfZ4x": "Suppleness",
    "-KgsjZPzi754NLdGfZ4a": "Western riding",
    "-KgsjZPzi754NLdGfZ4c": "Groundwork",
    "-KgsjZPzi754NLdGfZ4i": "Cross country",
    "-KgsjZPzi754NLdGfZ4z": "Endurance riding",
    "-KgsjZPzi754NLdGfZ4l": "Hunting",
    "-KgsjZPzi754NLdGfZ4m": "Reining",
    "-KgsjZPzi754NLdGfZ4n": "Barrel racing",
    "-KgsjZPzi754NLdGfZ4b": "Western pleasure",
    "-KgsjZPzi754NLdGfZ4d": "Working equitation",
    "-KgsjZPzi754NLdGfZ4e": "Polocrosse",
    "-KgsjZPzi754NLdGfZ4g": "Polo",
    "-KgsjZPzi754NLdGfZ5a": "Vaulting",
    "-KgsjZPzi754NLdGfZ5b": "Cutting",
    "-KgsjZPzi754NLdGfZ5c": "Mounted games",
    "-KgsjZPzi754NLdGfZ5d": "Hunter jumping",
    "-KgsjZPzi754NLdGfZ5e": "Harness racing",
    "-KgsjZPzi754NLdGfZ5f": "Combined driving",
    "-KgsjZPzi754NLdGfZ5g": "Icelandic horse riding",
    "-KgsjZPzi754NLdGfZ4f": "Other",
}
GAITS: tuple[str, ...] = ("stand", "walk", "trot", "canter", "tolt", "unknown")


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def best_records(data: dict[str, Any]) -> dict[str, float | None]:
    """Equilab's reported personal bests, not estimates from accessible history."""
    records = data.get("records")
    if not isinstance(records, list):
        return {}
    result: dict[str, float | None] = {}
    for kind in ("distance", "duration", "speed"):
        values = [
            number(r.get("value")) for r in records if isinstance(r, dict) and r.get("type") == kind
        ]
        result["record_" + kind] = max((v for v in values if v is not None), default=None)
    return result


@dataclass(frozen=True)
class Training:
    id: str
    start: datetime | None
    kind: str | None
    duration: float | None
    distance: float | None
    energy: float | None
    gaits: dict[str, float | None] = field(default_factory=dict)
    stats: dict[str, float | None] = field(default_factory=dict)
    rider_id: str | None = None
    horse_id: str | None = None
    rider_energy: float | None = None

    @classmethod
    def parse(cls, identifier: str, data: dict[str, Any]) -> Training:
        start = data.get("date")
        if not isinstance(start, datetime) or start.tzinfo is None:
            start = None
        total = data.get("total")
        total = total if isinstance(total, dict) else {}
        kinds = sorted({TYPE_NAMES.get(k, "Other") for k in active_ids(data.get("trainingTypes"))})
        gaits: dict[str, float | None] = {}
        stats: dict[str, float | None] = {}
        for gait in GAITS:
            values = data.get(gait)
            gaits[gait] = number(values.get("time")) if isinstance(values, dict) else None
        for gait in (*GAITS, "total"):
            values = data.get(gait)
            if not isinstance(values, dict):
                continue
            for source, target, multiplier in (
                ("pace", "speed", 1),
                ("topSpeed", "top_speed", 1),
                ("beat", "tempo", 60),
                ("counter", "strides", 1),
                ("stride", "stride_length", 1),
                ("distance", "distance", 1),
                ("leftTurnsDuration", "left_rein", 1),
                ("rightTurnsDuration", "right_rein", 1),
            ):
                value = number(values.get(source))
                stats[f"{gait}_{target}"] = value * multiplier if value is not None else None
        weather = data.get("weather")
        if isinstance(weather, dict):
            for key in ("temperature", "apparentTemperature", "windSpeed", "humidity", "pressure"):
                value = weather.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    if key == "humidity":
                        value = value * 100 if 0 <= value <= 1 else None
                    stats[f"weather_{key}"] = value
        transitions = data.get("transitionData")
        if isinstance(transitions, dict):
            counts = [
                number(v)
                for row in transitions.values()
                if isinstance(row, dict)
                for v in row.values()
            ]
            stats["transitions"] = (
                sum(cast(float, value) for value in counts)
                if all(value is not None for value in counts)
                else None
            )
        return cls(
            identifier,
            start,
            ", ".join(kinds) or None,
            number(total.get("time")),
            number(total.get("distance")),
            number(data.get("horseEnergyMj")),
            gaits,
            stats,
            text(data.get("user")),
            text(data.get("horse")),
            number(data.get("riderEnergy")),
        )

    @property
    def end(self) -> datetime | None:
        if self.start is None or self.duration is None:
            return None
        try:
            return self.start + timedelta(seconds=max(1, self.duration))
        except OverflowError:
            return None


@dataclass(frozen=True)
class Horse:
    id: str
    name: str
    breed: str | None
    birthday: str | None
    category: str | None
    level: str | None
    owned: bool
    trainings: tuple[Training, ...]
    skipped: int = 0
    profile: dict[str, Any] = field(default_factory=dict)

    @property
    def latest(self) -> Training | None:
        candidates = [training for training in self.trainings if training.start is not None]
        return max(candidates, key=lambda training: cast(datetime, training.start), default=None)

    def aggregate(self, period: str, metric: str, now: datetime) -> float | int | None:
        """Calendar periods in HA's timezone; missing data never becomes zero."""
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (
            start - timedelta(days=start.weekday()) if period == "week" else start.replace(day=1)
        )
        if self.skipped or any(t.start is None for t in self.trainings):
            return None
        items = [
            training
            for training in self.trainings
            if training.start is not None and start <= training.start <= now
        ]
        if metric == "count":
            return len(items)
        values = [getattr(t, metric) for t in items]
        return sum(values) if all(v is not None for v in values) else None


def horse_from_data(
    identifier: str,
    data: dict[str, Any],
    owned: bool,
    trainings: tuple[Training, ...],
    skipped: int,
) -> Horse:
    birth = data.get("birthDate")
    birthday = birth.date().isoformat() if isinstance(birth, datetime) else None
    if isinstance(birth, str):
        try:
            birthday = date.fromisoformat(birth[:10]).isoformat()
        except ValueError:
            pass
    return Horse(
        identifier,
        text(data.get("name")) or "Horse",
        text(data.get("horseBreed")),
        birthday,
        text(data.get("category")),
        text(data.get("level")),
        owned,
        trainings,
        skipped,
        {
            **best_records(data),
            "owner_id": text(data.get("ownerId")),
            "stable_id": text(data.get("stableId")),
            "weight": number(data.get("weight")),
            "tack_weight": number(data.get("toolWeight")),
            "records_count": len(data["records"])
            if isinstance(data.get("records"), list)
            else None,
        },
    )


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    model: str
    values: dict[str, Any]
    trainings: tuple[Training, ...] = ()
    skipped: int = 0

    latest = Horse.latest
    aggregate = Horse.aggregate


def rider_from_data(
    identifier: str,
    data: dict[str, Any],
    trainings: tuple[Training, ...],
    skipped: int,
    notifications: list[dict[str, Any]],
) -> Profile:
    values: dict[str, Any] = {
        key: text(data.get(key)) for key in ("firstName", "lastName", "city", "discipline")
    }
    values.update(best_records(data))
    for key in (
        "totalTrainingCount",
        "totalTrainingDistance",
        "totalTrainingTime",
        "unseenNotificationCount",
    ):
        values[key] = number(data.get(key))
    for key in ("achievements", "records"):
        values[key + "Count"] = len(data[key]) if isinstance(data.get(key), (dict, list)) else None
    latest = max(
        (n for n in notifications if isinstance(n.get("date"), datetime) and n["date"].tzinfo),
        key=lambda n: n["date"],
        default={},
    )
    values["lastNotification"] = latest.get("date")
    values["lastNotificationType"] = text(latest.get("type"))
    name = " ".join(filter(None, (values["firstName"], values["lastName"]))) or "Rider"
    return Profile(identifier, name, "Rider", values, tuple(trainings), skipped)


def stable_from_data(identifier: str, data: dict[str, Any]) -> Profile:
    values: dict[str, Any] = {key: text(data.get(key)) for key in ("type", "privacy", "ownerId")}
    values["horseCount"] = (
        len(active_ids(data["horses"])) if isinstance(data.get("horses"), dict) else None
    )
    for key, maximum in (("lat", 90), ("lon", 180)):
        value = data.get(key)
        values[key] = (
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and abs(value) <= maximum
            else None
        )
    return Profile(
        identifier, text(data.get("name")) or "Stable or group", "Stable / Group", values
    )
