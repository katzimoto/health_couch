"""Offline tests for the web dashboard (Starlette TestClient, no network)."""

from __future__ import annotations

import importlib
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    # Reload config and every module that captured `settings` at import time
    # (database, analysis) before webapp, or Database() falls back to a stale
    # db_path from whichever process/test imported it first — which can be a
    # real, non-temp database if one happens to be mounted at that path.
    import garmin_coach.config as config
    importlib.reload(config)
    import garmin_coach.storage.database as database
    importlib.reload(database)
    import garmin_coach.domain.analysis as analysis
    importlib.reload(analysis)
    import garmin_coach.surfaces.webapp as webapp
    importlib.reload(webapp)

    # Seed a couple of days directly through the app's DB handle.
    for i in range(5, 0, -1):
        d = date.today() - timedelta(days=i)
        webapp.db.upsert_sleep(d, score=80, total_seconds=7 * 3600)
        webapp.db.upsert_hrv(d, last_night_avg=60)
    return TestClient(webapp.build_app())


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Health Coach" in res.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_report_and_metric(client):
    report = client.get("/api/report").json()
    assert report["available"] is True
    series = client.get("/api/metric/hrv?days=30").json()
    assert len(series) == 5
    assert series[-1]["value"] == 60


def test_unknown_metric_404(client):
    assert client.get("/api/metric/;drop").status_code == 404


def test_static_assets(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_token_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "web2.db"))
    monkeypatch.setenv("DASHBOARD_TOKEN", "sekret")
    import garmin_coach.config as config
    importlib.reload(config)
    import garmin_coach.storage.database as database
    importlib.reload(database)
    import garmin_coach.domain.analysis as analysis
    importlib.reload(analysis)
    import garmin_coach.surfaces.webapp as webapp
    importlib.reload(webapp)

    c = TestClient(webapp.build_app())
    assert c.get("/api/report").status_code == 401
    assert c.get("/api/report?token=nope").status_code == 401
    assert c.get("/api/report?token=sekret").status_code == 200
    assert c.get("/healthz").status_code == 200  # health is always open
    # Static assets must load without a token, or a gated page renders blank:
    # the <link>/<script> tags that fetch them can't attach ?token=...
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/static/app.js").status_code == 200

    # Restore an unauthenticated module state for other tests.
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(analysis)
    importlib.reload(webapp)
