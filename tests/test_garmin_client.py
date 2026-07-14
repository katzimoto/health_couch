"""Offline tests for the Garmin puller (fake API, no network).

The extractors parse Garmin's undocumented JSON, which shifts between devices
and firmware — these fixtures pin the shapes we rely on so a ``garminconnect``
upgrade that breaks parsing fails here instead of silently in production.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import garmin_coach.ingest.garmin_client as garmin_client
from garmin_coach.storage.database import Database
from garmin_coach.ingest.garmin_client import GarminClient


class FakeGarminApi:
    """Canned responses mirroring real Garmin Connect payload shapes."""

    def get_sleep_data(self, day):
        return {
            "dailySleepDTO": {
                "sleepScores": {"overall": {"value": 82}},
                "sleepTimeSeconds": 7 * 3600,
                "deepSleepSeconds": 5400,
                "lightSleepSeconds": 14400,
                "remSleepSeconds": 5400,
                "awakeSleepSeconds": 1800,
            },
            "restingHeartRate": 48,
        }

    def get_hrv_data(self, day):
        return {"hrvSummary": {"lastNightAvg": 62, "weeklyAvg": 60, "status": "BALANCED"}}

    def get_rhr_day(self, day):
        return {
            "allMetrics": {
                "metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 47}]}
            }
        }

    def get_stress_data(self, day):
        return {"avgStressLevel": 30, "maxStressLevel": 80, "restStressDuration": 20000}

    def get_body_battery(self, start, end):
        return [
            {
                "highestBatteryLevel": 90,
                "lowestBatteryLevel": 20,
                "charged": 70,
                "drained": 60,
            }
        ]

    def get_user_summary(self, day):
        return {
            "totalSteps": 9000,
            "dailyStepGoal": 10000,
            "totalDistanceMeters": 7000.0,
            "totalKilocalories": 2400,
            "floorsAscended": 10,
        }

    def get_weigh_ins(self, start, end):
        return {
            "dailyWeightSummaries": [
                {
                    "latestWeight": {
                        "weight": 78500,  # grams
                        "bodyFat": 18.5,
                        "muscleMass": 35000,
                        "bodyWater": 55.0,
                        "bmi": 23.4,
                    }
                }
            ]
        }

    def get_hydration_data(self, day):
        return {"valueInML": 1500, "goalInML": 2500}

    def get_activities_by_date(self, start, end):
        return [
            {
                "activityId": 111,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "duration": 1800.0,
                "distance": 5000.0,
                "calories": 320,
                "averageHR": 150,
                "maxHR": 175,
                "activityTrainingLoad": 85.0,
            }
        ]


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "garmin.db"))


@pytest.fixture()
def client(db, monkeypatch) -> GarminClient:
    monkeypatch.setattr(garmin_client, "PULL_PAUSE_SECONDS", 0)
    c = GarminClient(db)
    c.api = FakeGarminApi()  # already "logged in"
    return c


def test_pull_day_extracts_every_family(client: GarminClient, db: Database) -> None:
    day = date.today().isoformat()
    results = client.pull_day(day)
    assert all(status == "ok" for status in results.values())

    row = db.latest_summary()
    assert row["sleep_score"] == 82
    assert row["sleep_hours"] == 7.0
    assert row["hrv"] == 62
    assert row["resting_hr"] == 47  # dedicated RHR endpoint wins over sleep's
    assert row["avg_stress"] == 30
    assert row["body_battery_high"] == 90
    assert row["steps"] == 9000
    assert row["weight_kg"] == 78.5  # grams converted
    assert row["hydration_ml"] == 1500
    assert row["workout_count"] == 1
    assert row["training_load"] == 85.0


def test_one_broken_endpoint_does_not_abort_the_pull(client: GarminClient, db: Database) -> None:
    def boom(_day):
        raise RuntimeError("Garmin 500")

    client.api.get_hrv_data = boom
    day = date.today().isoformat()
    results = client.pull_day(day)
    assert results["hrv"].startswith("error")
    assert results["sleep"] == "ok"
    assert db.latest_summary()["sleep_score"] == 82
    # Partial success still counts as pulled — the data that exists is in.
    assert day in db.pulled_days(day, day)


def test_fully_failed_day_stays_missing_for_retry(client: GarminClient, db: Database) -> None:
    class DeadApi:
        def __getattr__(self, name):
            def fail(*_a, **_kw):
                raise RuntimeError("auth expired")
            return fail

    client.api = DeadApi()
    day = date.today().isoformat()
    results = client.pull_day(day)
    assert all(status.startswith("error") for status in results.values())
    assert db.pulled_days(day, day) == set()  # gap healing will retry it


def test_pull_missing_days_heals_oldest_gaps_first(client: GarminClient, db: Database) -> None:
    today = date.today()
    # Window of 4 days (5 expected incl. today); mark today + yesterday pulled.
    for offset in (0, 1):
        db.record_pull(today - timedelta(days=offset), {"sleep": "ok"})

    healed = client.pull_missing_days(window_days=4, limit=2)

    expected = [(today - timedelta(days=o)).isoformat() for o in (4, 3)]
    assert healed == expected  # oldest first, bounded by limit
    pulled = db.pulled_days(today - timedelta(days=4), today)
    assert set(expected) <= pulled
    # The remaining gap is left for the next run.
    assert (today - timedelta(days=2)).isoformat() not in pulled
