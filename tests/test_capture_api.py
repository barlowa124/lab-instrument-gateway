"""Capture service and API tests."""
import math

import pytest
from fastapi.testclient import TestClient

from lablink.api import create_app
from lablink.capture import AlarmRule, CaptureService
from lablink.driver import InstrumentClient
from lablink.simulator import serve


@pytest.fixture()
def device(tmp_path):
    srv = serve(port=0)
    host, port = srv.server_address
    client = InstrumentClient(host, port)
    client.connect()
    yield client, srv, tmp_path / "test.db"
    client.close()
    srv.shutdown()


def test_poll_writes_typed_rows(device):
    client, srv, db = device
    svc = CaptureService(client, str(db))
    rows = svc.poll_once()
    assert len(rows) == 5
    assert all(r.quality == "ok" for r in rows)
    latest = svc.latest()
    assert {r["channel"] for r in latest} == {"TEMP", "PH", "DO", "AGIT", "WEIGHT"}
    svc.stop()


def test_transport_fault_still_writes_error_rows(device):
    client, srv, db = device
    svc = CaptureService(client, str(db))
    srv.device.fault = "DROP"
    rows = svc.poll_once()
    assert all(r.quality == "transport-error" for r in rows)
    assert svc.stats["transport_error"] == 5
    svc.stop()


def test_alarm_rule_fires_and_persists(device):
    client, srv, db = device
    svc = CaptureService(client, str(db),
                         rules=[AlarmRule("TEMP", 40.0, 41.0, "temp-too-low")])
    svc.poll_once()
    al = svc.alarms()
    assert al and al[0]["rule"] == "temp-too-low"
    svc.stop()


def test_api_endpoints(device):
    client, srv, db = device
    app = create_app(client, str(db), api_token="test-token")
    auth = {"Authorization": "Bearer test-token"}
    with TestClient(app) as tc:
        assert tc.get("/api/instrument").json()["connected"] is True
        assert "TEMP" in tc.get("/api/channels").json()
        svc_rows = tc.get("/api/latest").json()
        assert len(svc_rows) == 5
        hist = tc.get("/api/history/TEMP").json()
        assert hist and isinstance(hist[0]["value"], float) and math.isfinite(hist[0]["value"])
        assert tc.get("/api/history/TEMP?limit=99999").status_code == 422
        r = tc.post("/api/setpoint/PH", params={"value": 7.4}, headers=auth)
        assert r.json()["ok"] is True
        assert tc.get("/api/history/NOPE").status_code == 404


def test_setpoint_requires_bearer_token(device):
    client, srv, db = device
    app = create_app(client, str(db), api_token="test-token")
    with TestClient(app) as tc:
        assert tc.post("/api/setpoint/PH", params={"value": 7.4}).status_code == 401
        assert tc.post("/api/setpoint/PH", params={"value": 7.4},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = tc.post("/api/setpoint/PH", params={"value": 7.4},
                    headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200
        assert srv.device.setpoints["PH"] == pytest.approx(7.4)


def test_setpoint_rejects_foreign_origin(device):
    client, srv, db = device
    app = create_app(client, str(db), api_token="test-token")
    with TestClient(app) as tc:
        r = tc.post("/api/setpoint/PH", params={"value": 7.4},
                    headers={"Authorization": "Bearer test-token",
                             "Origin": "http://evil.example"})
        assert r.status_code == 403
        assert srv.device.setpoints["PH"] == pytest.approx(7.2)


def test_setpoint_disabled_without_token(device):
    client, srv, db = device
    app = create_app(client, str(db))
    with TestClient(app) as tc:
        assert tc.post("/api/setpoint/PH", params={"value": 7.4}).status_code == 403
        assert srv.device.setpoints["PH"] == pytest.approx(7.2)


def test_setpoint_rejects_nonfinite(device):
    client, srv, db = device
    app = create_app(client, str(db), api_token="t")
    with TestClient(app) as tc:
        for bad in ("nan", "inf", "-inf"):
            r = tc.post("/api/setpoint/TEMP", params={"value": bad},
                        headers={"Authorization": "Bearer t"})
            assert r.status_code in (400, 422)
        assert srv.device.setpoints["TEMP"] == pytest.approx(37.0)


def test_nonfinite_reading_stored_as_device_error(device):
    client, srv, db = device
    real_query = client.query
    client.query = lambda line: "nan" if line.startswith("MEAS:") else real_query(line)
    svc = CaptureService(client, str(db))
    rows = svc.poll_once()
    assert all(r.quality == "device-error" for r in rows)
    assert svc.stats["device_error"] == 5
    latest = svc.latest()
    assert all(r["value"] is None and r["quality"] == "device-error" for r in latest)
    assert svc.alarms() == []
    svc.stop()


def test_api_latest_serializes_error_rows(device):
    client, srv, db = device
    real_query = client.query
    client.query = lambda line: "inf" if line.startswith("MEAS:") else real_query(line)
    app = create_app(client, str(db), api_token="t")
    with TestClient(app) as tc:
        rows = tc.get("/api/latest").json()
        assert len(rows) == 5
        assert all(r["quality"] == "device-error" and r["value"] is None for r in rows)


def test_stop_waits_for_inflight_poll(device):
    import threading
    import time

    client, srv, db = device
    svc = CaptureService(client, str(db))
    gate = threading.Event()
    gate.set()
    orig_measure = client.measure

    def slow_measure(ch):
        gate.wait(5)
        return orig_measure(ch)

    client.measure = slow_measure
    svc.start(interval_s=0.02)
    gate.clear()
    time.sleep(0.05)
    assert svc._thread.is_alive()
    threading.Timer(0.3, gate.set).start()
    svc.stop()
    # the in-flight poll must finish before stop returns. The old code closed
    # the database while the worker was still running
    assert svc._thread is None
    assert svc._closed


def test_stop_joins_worker_without_timeout(device):
    """A bounded join lets a stuck poll outlive the closed database."""
    client, srv, db = device
    svc = CaptureService(client, str(db))
    joins = []

    class RecordWorker:
        def join(self, timeout=None):
            joins.append(timeout)

    svc._thread = RecordWorker()
    svc.stop()
    svc.stop()  # idempotent
    assert joins == [None]
    assert svc._closed


def test_nan_rows_do_not_poison_latest(device):
    client, srv, db = device
    svc = CaptureService(client, str(db))
    svc.poll_once()
    srv.device.fault = "DROP"
    svc.poll_once()
    latest = {r["channel"]: r for r in svc.latest()}
    assert latest["TEMP"]["quality"] == "transport-error"
    assert latest["TEMP"]["value"] is None
    svc.stop()
