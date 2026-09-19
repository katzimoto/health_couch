"""Tests for structured multi-source workout metrics (issue #19).

The headline case is the issue's own acceptance test: one indoor-cycling
session recorded by a Garmin watch (heart rate, training load) and a Star Trac
bike console (duration, distance, calories, power, cadence, METs) that never
talked to each other. Everything here is offline over synthetic fixtures.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.database import Database
from garmin_coach.workout_merge import merge_fields
from garmin_coach.workout_metrics import (
    CONFLICT_TOLERANCE,
    DEFAULT_PRIORITY,
    canonical_unit,
    metric_key,
    normalize_metric,
    normalize_observations,
    normalize_source,
    observation_identity,
    rule_for,
    select_metrics,
    source_bucket,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "metrics.db"))


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


# ── Normalization ──────────────────────────────────────────────────────────────

def test_metric_names_and_aggregations_normalize() -> None:
    assert normalize_metric("avg_power") == ("power", "avg")
    assert normalize_metric("max_cadence") == ("cadence", "max")
    assert normalize_metric("watts", "maximum") == ("power", "max")
    assert normalize_metric("rpm") == ("cadence", "avg")
    assert normalize_metric("METs") == ("mets", "avg")
    assert normalize_metric("HR", "max") == ("heart_rate", "max")
    # An explicit aggregation wins over the prefix.
    assert normalize_metric("avg_power", "max") == ("power", "max")
    # Unknown metrics survive — the representation is extensible.
    assert normalize_metric("flywheel torque") == ("flywheel_torque", "avg")


def test_units_are_recorded_not_converted() -> None:
    assert canonical_unit("power") == "W"
    assert canonical_unit("cadence") == "rpm"
    assert canonical_unit("mets") == "METs"
    # A caller's unit is kept verbatim, including for unknown metrics.
    assert canonical_unit("power", "kW") == "kW"
    assert canonical_unit("flywheel_torque", "Nm") == "Nm"
    assert canonical_unit("flywheel_torque") is None


def test_source_buckets_recognise_equipment() -> None:
    assert source_bucket("star_trac") == "equipment"
    assert source_bucket("Star Trac") == "equipment"
    assert source_bucket("concept2") == "equipment"
    assert source_bucket("technogym console") == "equipment"
    assert source_bucket("garmin") == "garmin"
    assert source_bucket("apple_health") == "apple"
    assert source_bucket("photo") == "photo"
    # An unrecognised name is treated as manual — the least authoritative.
    assert source_bucket("some_new_app") == "manual"
    assert normalize_source("Star Trac") == "star_trac"


def test_mapping_and_list_forms_both_normalize() -> None:
    from_mapping, rejected = normalize_observations(
        {"avg_power": 82, "avg_cadence": 54}, default_source="star_trac"
    )
    assert rejected == []
    assert {o["metric"] for o in from_mapping} == {"power", "cadence"}
    assert all(o["source"] == "star_trac" for o in from_mapping)

    from_list, rejected = normalize_observations(
        [{"metric": "power", "value": 82, "unit": "W", "aggregation": "avg"}],
        default_source="star_trac",
    )
    assert rejected == [] and from_list[0]["unit"] == "W"


def test_unusable_entries_are_reported_not_dropped() -> None:
    observations, rejected = normalize_observations(
        [{"metric": "power", "value": "not a number"}, {"value": 5}],
        default_source="star_trac",
    )
    assert observations == []
    assert len(rejected) == 2
    assert all("numeric value" in r["reason"] for r in rejected)


# ── Selection rules ────────────────────────────────────────────────────────────

def _observation(metric: str, value: float, source: str, **extra) -> dict:
    observations, _ = normalize_observations(
        [{"metric": metric, "value": value, **extra}], default_source=source
    )
    return observations[0]


def test_watch_wins_heart_rate_machine_wins_power() -> None:
    resolved = select_metrics([
        _observation("avg_heart_rate", 150, "garmin"),
        _observation("avg_heart_rate", 138, "star_trac"),
        _observation("avg_power", 82, "star_trac"),
        _observation("avg_power", 90, "garmin"),
    ])
    assert resolved["heart_rate:avg"]["value"] == 150
    assert resolved["heart_rate:avg"]["source"] == "garmin"
    assert resolved["power:avg"]["value"] == 82
    assert resolved["power:avg"]["source"] == "star_trac"
    assert "rule" in resolved["power:avg"]["selection_reason"]


def test_losing_observations_are_kept_and_conflicts_flagged() -> None:
    resolved = select_metrics([
        _observation("avg_heart_rate", 150, "garmin"),
        _observation("avg_heart_rate", 138, "star_trac"),
    ])
    entry = resolved["heart_rate:avg"]
    assert entry["conflict"] is True
    assert entry["alternatives"][0]["value"] == 138
    assert entry["alternatives"][0]["source"] == "star_trac"
    assert "both values are kept" in entry["conflict_note"]


def test_near_identical_readings_are_not_called_a_conflict() -> None:
    resolved = select_metrics([
        _observation("avg_power", 82, "star_trac"),
        _observation("avg_power", 82 * (1 + CONFLICT_TOLERANCE / 2), "garmin"),
    ])
    assert resolved["power:avg"]["conflict"] is False
    assert resolved["power:avg"]["alternatives"]  # still kept


def test_unknown_metrics_follow_the_documented_default_rule() -> None:
    assert rule_for("flywheel_torque") == DEFAULT_PRIORITY
    resolved = select_metrics([
        _observation("flywheel_torque", 12, "manual", unit="Nm"),
        _observation("flywheel_torque", 14, "star_trac", unit="Nm"),
    ])
    assert resolved["flywheel_torque:avg"]["source"] == "star_trac"


def test_explicit_override_beats_the_rules() -> None:
    observations = [
        _observation("avg_heart_rate", 150, "garmin"),
        _observation("avg_heart_rate", 138, "star_trac"),
    ]
    resolved = select_metrics(observations, overrides={"heart_rate": "star_trac"})
    assert resolved["heart_rate:avg"]["value"] == 138
    assert "manual override" in resolved["heart_rate:avg"]["selection_reason"]


def test_selection_is_deterministic_within_a_bucket() -> None:
    a = _observation("avg_power", 80, "star_trac", confidence=0.5)
    b = _observation("avg_power", 82, "keiser", confidence=0.9)
    assert select_metrics([a, b])["power:avg"]["value"] == 82   # higher confidence
    assert select_metrics([b, a])["power:avg"]["value"] == 82   # order-independent


def test_observation_identity_is_what_dedupes_a_re_import() -> None:
    first = _observation("avg_power", 82, "star_trac")
    again = _observation("avg_power", 84, "star_trac")
    assert observation_identity(first) == observation_identity(again)
    assert observation_identity(first) != observation_identity(
        _observation("avg_power", 82, "garmin")
    )
    assert metric_key("power", "avg") == "power:avg"


# ── Field-level merge priority ─────────────────────────────────────────────────

def test_equipment_wins_distance_duration_calories_watch_wins_hr() -> None:
    garmin = {"activity_id": 1, "duration_s": 895, "avg_hr": 150, "max_hr": 166,
              "training_load": 27.9, "source": "garmin", "load_source": "garmin",
              "type": "indoor_cycling"}
    machine = {"activity_id": -1, "duration_s": 895, "distance_m": 4700,
               "calories": 93, "source": "star_trac", "type": "indoor_cycling"}
    merged, provenance = merge_fields(
        {"garmin": garmin, "equipment": machine}, is_strength=False
    )
    assert provenance["duration_s"] == "equipment"
    assert provenance["distance_m"] == "equipment"
    assert provenance["calories"] == "equipment"
    assert provenance["avg_hr"] == "garmin"
    assert provenance["max_hr"] == "garmin"
    assert provenance["training_load"] == "garmin"
    assert merged["distance_m"] == 4700 and merged["avg_hr"] == 150


def test_existing_manual_garmin_priorities_are_unchanged() -> None:
    """The equipment bucket must not disturb the pre-existing rules."""
    garmin = {"activity_id": 1, "duration_s": 3200, "avg_hr": 128, "calories": 400,
              "distance_m": 100, "training_load": 90.0, "source": "garmin",
              "load_source": "garmin", "type": "strength_training"}
    manual = {"activity_id": -1, "duration_s": 3300, "calories": 350,
              "training_load": 70.0, "source": "manual", "type": "strength_training"}
    merged, provenance = merge_fields({"manual": manual, "garmin": garmin}, is_strength=True)
    assert provenance["duration_s"] == "garmin"
    assert provenance["calories"] == "garmin"
    assert provenance["distance_m"] == "garmin"
    assert merged["training_load"] == 90.0
    assert provenance["exercise_details"] == "manual"


# ── Persistence ────────────────────────────────────────────────────────────────

def test_metrics_persist_with_units_aggregation_and_provenance(db: Database) -> None:
    db.upsert_workout(-1, _day(), name="Bike", type="indoor_cycling",
                      duration_s=895, source="star_trac")
    result = db.upsert_workout_metrics(
        -1, {"avg_power": 82, "max_power": 110, "avg_cadence": 54, "avg_mets": 5.1},
        source="star_trac", source_ref="console-42",
    )
    assert result["stored"] == 4 and result["rejected"] == []

    metrics = db.get_workout_metrics(-1)
    power = metrics["selected"]["power:avg"]
    assert (power["value"], power["unit"], power["aggregation"]) == (82, "W", "avg")
    assert power["source"] == "star_trac" and power["source_bucket"] == "equipment"
    assert metrics["selected"]["power:max"]["value"] == 110
    assert metrics["selected"]["mets:avg"]["unit"] == "METs"
    assert all(o["source_ref"] == "console-42" for o in metrics["observations"])


def test_re_import_updates_in_place_and_never_double_counts(db: Database) -> None:
    db.upsert_workout(-2, _day(), name="Bike", type="indoor_cycling", source="star_trac")
    db.upsert_workout_metrics(-2, {"avg_power": 82}, source="star_trac")
    db.upsert_workout_metrics(-2, {"avg_power": 84}, source="star_trac")
    metrics = db.get_workout_metrics(-2)
    assert metrics["observation_count"] == 1
    assert metrics["selected"]["power:avg"]["value"] == 84


def test_two_sources_of_one_metric_are_both_stored(db: Database) -> None:
    db.upsert_workout(-3, _day(), name="Bike", type="indoor_cycling", source="star_trac")
    db.upsert_workout_metrics(-3, {"avg_heart_rate": 138}, source="star_trac")
    db.upsert_workout_metrics(-3, {"avg_heart_rate": 150}, source="garmin")
    metrics = db.get_workout_metrics(-3)
    assert metrics["observation_count"] == 2
    assert metrics["selected"]["heart_rate:avg"]["value"] == 150  # watch wins
    assert metrics["conflicts"] == ["heart_rate:avg"]
    selected_rows = [o for o in metrics["observations"] if o["is_selected"]]
    assert len(selected_rows) == 1 and selected_rows[0]["source"] == "garmin"


def test_manual_override_persists_and_can_be_changed(db: Database) -> None:
    db.upsert_workout(-4, _day(), name="Bike", type="indoor_cycling", source="star_trac")
    db.upsert_workout_metrics(-4, {"avg_heart_rate": 138}, source="star_trac")
    db.upsert_workout_metrics(-4, {"avg_heart_rate": 150}, source="garmin")

    override = db.set_workout_metric_source(-4, "heart_rate", "star_trac")
    assert override["updated"] is True
    assert db.get_workout_metrics(-4)["selected"]["heart_rate:avg"]["value"] == 138

    back = db.set_workout_metric_source(-4, "avg_heart_rate", "garmin")
    assert back["updated"] is True
    assert db.get_workout_metrics(-4)["selected"]["heart_rate:avg"]["value"] == 150
    # Both observations survived the overriding.
    assert db.get_workout_metrics(-4)["observation_count"] == 2


def test_override_of_an_unrecorded_source_is_refused(db: Database) -> None:
    db.upsert_workout(-5, _day(), name="Bike", type="indoor_cycling", source="star_trac")
    db.upsert_workout_metrics(-5, {"avg_power": 82}, source="star_trac")
    result = db.set_workout_metric_source(-5, "power", "garmin")
    assert result["updated"] is False
    assert result["available_sources"] == ["star_trac"]
    result = db.set_workout_metric_source(-5, "cadence", "star_trac")
    assert result["updated"] is False and "no cadence:avg observation" in result["error"]


def test_schema_is_created_on_an_existing_database(tmp_path) -> None:
    """A live database gains the metric table on the next startup."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    Database(path=path)  # first boot creates everything
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE workout_metric")
    Database(path=path)  # a later boot recreates the missing table
    with sqlite3.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(workout_metric)")}
    assert {"activity_id", "metric", "aggregation", "value", "unit", "source",
            "source_activity_id", "confidence", "is_selected", "is_override"} <= columns


# ── The issue's acceptance test ────────────────────────────────────────────────

def _garmin_and_star_trac(db: Database) -> tuple[int, int]:
    """The two observations from the issue, as two workout rows."""
    start = f"{_day()} 18:00:00"
    db.upsert_workout(
        900, _day(), name="Indoor cycling", type="indoor_cycling",
        duration_s=895, avg_hr=150, max_hr=166, training_load=27.9,
        source="garmin", load_source="garmin", start_time=start,
    )
    db.upsert_workout(
        -900, _day(), name="Star Trac bike", type="indoor_cycling",
        duration_s=895, distance_m=4700, calories=93,
        source="star_trac", start_time=start,
    )
    db.upsert_workout_metrics(
        -900, {"avg_power": 82, "avg_cadence": 54, "avg_mets": 5.1}, source="star_trac",
    )
    return 900, -900


def test_acceptance_one_canonical_workout_with_per_field_sources(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    result = db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])
    assert result["merged"] is True
    merge = result["merges"][0]
    canonical_id = merge["canonical_activity_id"]

    canonical = db.get_merged_workout(canonical_id)
    summary, sources = canonical["canonical"], canonical["field_sources"]

    # Every selected value is present on one canonical workout...
    assert summary["duration_s"] == 895
    assert summary["distance_m"] == 4700
    assert summary["calories"] == 93
    assert summary["avg_hr"] == 150
    assert summary["max_hr"] == 166
    assert summary["training_load"] == 27.9

    # ...and each field remembers which device supplied it.
    assert sources["duration_s"] == "equipment"
    assert sources["distance_m"] == "equipment"
    assert sources["calories"] == "equipment"
    assert sources["avg_hr"] == "garmin"
    assert sources["max_hr"] == "garmin"
    assert sources["training_load"] == "garmin"
    assert summary["load_source"] == "garmin"  # the load's actual origin

    # The structured metrics came along, with their own provenance.
    metrics = canonical["metrics"]["selected"]
    assert metrics["power:avg"]["value"] == 82
    assert metrics["power:avg"]["unit"] == "W"
    assert metrics["cadence:avg"]["value"] == 54
    assert metrics["mets:avg"]["value"] == pytest.approx(5.1)
    assert all(m["source"] == "star_trac" for m in metrics.values())


def test_acceptance_original_observations_remain_available(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])

    rows = {w["activity_id"]: w for w in db.recent_workouts(days=7, include_duplicates=True)}
    assert rows[garmin_id]["avg_hr"] == 150          # the watch's own row survives
    assert rows[machine_id]["distance_m"] == 4700    # so does the machine's
    assert rows[garmin_id]["duplicate_of"] == rows[machine_id]["duplicate_of"]

    canonical_id = rows[garmin_id]["duplicate_of"]
    linked = {s["source"]: s for s in db.get_merged_workout(canonical_id)["linked_sources"]}
    assert set(linked) == {"garmin", "equipment"}
    # Each metric observation still names the row it was measured on.
    observations = db.get_workout_metrics(canonical_id)["observations"]
    assert {o["source_activity_id"] for o in observations} == {machine_id}


def test_acceptance_counted_once_in_summaries_and_training_load(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])

    canonical = [w for w in db.recent_workouts(days=7)]
    assert len(canonical) == 1
    # Not 27.9 + anything, and not two sessions.
    assert canonical[0]["training_load"] == 27.9
    summary = [r for r in db.daily_summary(days=7) if r["day"] == _day()][0]
    assert summary["workout_count"] == 1
    assert summary["training_load"] == pytest.approx(27.9)


def test_acceptance_metrics_are_retrievable_through_the_tools(db: Database, monkeypatch) -> None:
    import garmin_coach.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "db", db)
    garmin_id, machine_id = _garmin_and_star_trac(db)
    db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])

    # Any source id resolves to the canonical session's metrics.
    via_machine = mcp_server.get_workout_metrics(machine_id)
    via_watch = mcp_server.get_workout_metrics(garmin_id)
    assert via_machine["activity_id"] == via_watch["activity_id"]
    assert via_machine["selected"]["cadence:avg"]["value"] == 54
    assert via_machine["selected"]["power:avg"]["selection_reason"]


def test_repeated_merge_does_not_duplicate_metrics(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    first = db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])
    canonical_id = first["merges"][0]["canonical_activity_id"]
    db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])
    db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])

    metrics = db.get_workout_metrics(canonical_id)
    assert metrics["observation_count"] == 3  # power, cadence, mets — once each
    assert len(db.recent_workouts(days=7)) == 1


def test_unmerge_returns_metrics_to_their_source_row(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    merged = db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])
    canonical_id = merged["merges"][0]["canonical_activity_id"]
    db.unmerge_workout_sources(canonical_id)

    restored = db.get_workout_metrics(machine_id)
    assert restored["activity_id"] == machine_id
    assert restored["selected"]["power:avg"]["value"] == 82
    assert {w["activity_id"] for w in db.recent_workouts(days=7)} == {garmin_id, machine_id}


def test_metrics_logged_against_a_merged_row_land_on_the_canonical(db: Database) -> None:
    garmin_id, machine_id = _garmin_and_star_trac(db)
    merged = db.merge_workout_sources(source_activity_ids=[garmin_id, machine_id])
    canonical_id = merged["merges"][0]["canonical_activity_id"]

    late = db.upsert_workout_metrics(
        machine_id, {"max_cadence": 61}, source="star_trac"
    )
    assert late["activity_id"] == canonical_id
    assert db.get_workout_metrics(canonical_id)["selected"]["cadence:max"]["value"] == 61


def test_log_workout_tool_stores_source_and_metrics(db: Database, monkeypatch) -> None:
    import garmin_coach.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "db", db)
    logged = mcp_server.log_workout(
        name="Star Trac bike", type="indoor_cycling", duration_s=895,
        distance_m=4700, calories=93, source="star_trac",
        metrics={"avg_power": 82, "avg_cadence": 54, "avg_mets": 5.1},
    )
    assert logged["source"] == "star_trac"
    selected = logged["metrics"]["metrics"]["selected"]
    assert selected["power:avg"]["value"] == 82
    assert selected["power:avg"]["source_bucket"] == "equipment"
