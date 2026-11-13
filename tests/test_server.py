import json
import threading
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest

from glow import server
from glow.config import load_config


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["paths"]["regions"] = tmp_path / "regions.json"
    cfg["paths"]["output"] = tmp_path / "out"
    cfg["paths"]["archive"] = tmp_path / "out" / "archive"
    cfg["paths"]["spots"] = tmp_path / "spots.json"
    cfg["paths"]["cache"] = tmp_path / "cache"
    computed = []

    def fake_run(cfg_, region, progress=lambda m: None):
        progress("计算霞光")
        time.sleep(0.2)
        computed.append((region.id, None))
        path = cfg_["paths"]["output"] / "regions" / region.id / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "generated_at": "2026-11-06T00:00:00+00:00",
            "events": {"sunset": [{"date": day} for day in ("2026-11-06", "2026-11-07", "2026-11-08")]},
        }), encoding="utf-8")

    def fake_run_day(cfg_, region, day, progress=lambda m: None):
        progress("计算霞光")
        time.sleep(0.2)
        computed.append((region.id, day))

    monkeypatch.setattr(server, "run_region", fake_run)
    monkeypatch.setattr(server, "run_region_day", fake_run_day)
    monkeypatch.setattr(server.regions, "timezone_at", lambda lat, lon: "Asia/Tokyo")
    httpd = server.make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", computed, cfg
    httpd.shutdown()


def call(base, path, method="GET", body=None, origin=None):
    req = Request(base + path, method=method, data=None if body is None else json.dumps(body).encode())
    if origin:
        req.add_header("Origin", origin)
    with urlopen(req) as r:
        return r.status, json.loads(r.read())


def wait_idle(base):
    for _ in range(50):
        if call(base, "/api/status")[1]["state"] == "idle":
            return
        time.sleep(0.05)
    raise AssertionError("worker did not finish")


def test_create_computes_and_lists_region(api):
    base, computed, _ = api
    status, r = call(base, "/api/regions", "POST", {"name": "仙台", "lat": 38.27, "lon": 140.87}, origin=base)
    assert status == 201 and r["name"] == "仙台" and r["display"]["step"] == 0.02
    assert r["display"]["lat_max"] - r["display"]["lat_min"] == 4.0
    wait_idle(base)
    assert computed == [(r["id"], None)]
    listed = call(base, "/api/regions")[1]
    assert [x["id"] for x in listed["regions"]] == [r["id"]] and listed["regions"][0]["pending"] is False
    assert listed["limits"]["frame"]["lon_span"] == 5.0
    assert listed["limits"]["forecast_days"] == 3


def test_rejects_frame_beyond_latitude_limit(api):
    base, computed, _ = api
    with pytest.raises(HTTPError) as e:
        call(base, "/api/regions", "POST", {"name": "北", "lat": 59.5, "lon": 10})
    assert e.value.code == 400 and "纬" in json.loads(e.value.read())["error"]
    assert not computed


def test_refresh_queues_one_region_day_once(api):
    base, computed, _ = api
    ids = [call(base, "/api/regions", "POST", {"name": n, "lat": 35, "lon": lo})[1]["id"] for n, lo in (("a", 135), ("b", 139))]
    wait_idle(base)
    computed.clear()
    body = {"region": ids[0], "date": "2026-11-07"}
    call(base, "/api/refresh", "POST", body)
    call(base, "/api/refresh", "POST", body)
    wait_idle(base)
    assert computed == [(ids[0], "2026-11-07")]


def test_refresh_requires_region_and_date(api):
    base, computed, _ = api
    rid = call(base, "/api/regions", "POST", {"name": "x", "lat": 35, "lon": 139})[1]["id"]
    wait_idle(base)
    computed.clear()
    for body in ({}, {"region": rid}, {"region": rid, "date": "2026/11/07"}):
        with pytest.raises(HTTPError) as e:
            call(base, "/api/refresh", "POST", body)
        assert e.value.code == 400
    assert not computed


def test_refresh_allows_only_future_dates_inside_horizon_when_missing(api):
    base, computed, cfg = api
    rid = call(base, "/api/regions", "POST", {"name": "x", "lat": 35, "lon": 139})[1]["id"]
    wait_idle(base)
    path = cfg["paths"]["output"] / "regions" / rid / "manifest.json"
    path.write_text(json.dumps({
        "generated_at": "2026-11-08T00:00:00+00:00",
        "events": {"sunset": [{"date": "2000-01-01"}]},
    }), encoding="utf-8")
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    computed.clear()

    future = (today + timedelta(days=1)).isoformat()
    call(base, "/api/refresh", "POST", {"region": rid, "date": future})
    wait_idle(base)
    assert computed == [(rid, future)]

    for day in ((today - timedelta(days=1)).isoformat(),
                (today + timedelta(days=4)).isoformat()):
        with pytest.raises(HTTPError) as e:
            call(base, "/api/refresh", "POST", {"region": rid, "date": day})
        assert e.value.code == 400


def test_delete_region(api):
    base, _, _ = api
    rid = call(base, "/api/regions", "POST", {"name": "x", "lat": 35, "lon": 139})[1]["id"]
    wait_idle(base)
    assert call(base, f"/api/regions/{rid}", "DELETE")[1] == {"deleted": rid}
    assert call(base, "/api/regions")[1]["regions"] == []


def test_state_changes_reject_other_origins(api):
    base, computed, _ = api
    for path, method in (("/api/refresh", "POST"), ("/api/regions", "POST"), ("/api/regions/abc", "DELETE"),
                         ("/api/archive/abc", "DELETE"), ("/api/archive/abc/snap", "DELETE")):
        with pytest.raises(HTTPError) as e:
            call(base, path, method, {}, origin="https://evil.example")
        assert e.value.code == 403
    assert not computed


def test_archive_api_lists_and_deletes_snapshot(api):
    base, _, cfg = api
    region = call(base, "/api/regions", "POST", {"name": "存档区", "lat": 35, "lon": 139})[1]
    wait_idle(base)
    snap = cfg["paths"]["archive"] / region["id"] / "snap1"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{}", encoding="utf-8")
    index = {
        "region": region,
        "snapshots": [{
            "snapshot": "snap1",
            "archived_at": "2026-11-06T00:00:00+00:00",
            "manifest": f"archive/{region['id']}/snap1/manifest.json",
        }],
    }
    (cfg["paths"]["archive"] / region["id"] / "index.json").write_text(
        json.dumps(index), encoding="utf-8"
    )

    listed = call(base, "/api/archive")[1]["regions"]
    assert [r["id"] for r in listed] == [region["id"]]
    assert [s["snapshot"] for s in listed[0]["snapshots"]] == ["snap1"]
    assert call(base, f"/api/archive/{region['id']}/snap1", "DELETE")[1]["scope"] == "snapshot"
    assert call(base, "/api/archive")[1]["regions"] == []


def test_deleting_region_keeps_permanent_archive(api):
    base, _, cfg = api
    region = call(base, "/api/regions", "POST", {"name": "留存区", "lat": 35, "lon": 139})[1]
    wait_idle(base)
    snap = cfg["paths"]["archive"] / region["id"] / "snap1"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{}", encoding="utf-8")
    index = {
        "region": region,
        "snapshots": [{
            "snapshot": "snap1",
            "archived_at": "2026-11-06T00:00:00+00:00",
            "manifest": f"archive/{region['id']}/snap1/manifest.json",
        }],
    }
    (cfg["paths"]["archive"] / region["id"] / "index.json").write_text(
        json.dumps(index), encoding="utf-8"
    )

    assert call(base, f"/api/regions/{region['id']}", "DELETE")[1] == {"deleted": region["id"]}
    assert call(base, "/api/regions")[1]["regions"] == []
    archived = call(base, "/api/archive")[1]["regions"]
    assert [r["id"] for r in archived] == [region["id"]]


def test_status_reports_monotonic_progress(api, monkeypatch):
    base, _, _ = api
    seen = []

    def fake_run(cfg_, region, progress=lambda m, f=None: None):
        progress("GFS 云量", 0.4)
        seen.append(call(base, "/api/status")[1])
        progress("Open-Meteo gfs 1/3", 0.2)  # fallback restarts its own count
        seen.append(call(base, "/api/status")[1])

    monkeypatch.setattr(server, "run_region", fake_run)
    call(base, "/api/regions", "POST", {"name": "仙台", "lat": 38.27, "lon": 140.87}, origin=base)
    wait_idle(base)
    assert [s["progress"] for s in seen] == [0.4, 0.4]
    assert seen[1]["stage"] == "Open-Meteo gfs 1/3" and seen[0]["started_at"]
    idle = call(base, "/api/status")[1]
    assert idle["progress"] is None and idle["started_at"] is None


def test_serves_results_from_output_directory(api):
    base, _, cfg = api
    path = cfg["paths"]["output"] / "regions" / "r1" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"ok": true}', encoding="utf-8")
    assert call(base, "/data/regions/r1/manifest.json") == (200, {"ok": True})
    with pytest.raises(HTTPError) as e:
        call(base, "/data/../../regions.json")
    assert e.value.code == 404


def test_spots_create_list_and_delete(api):
    base, _, _ = api
    status, spot = call(base, "/api/spots", "POST", {"name": " 西山 ", "lat": 24.96, "lon": 102.63}, origin=base)
    assert status == 201 and spot["name"] == "西山"
    assert call(base, "/api/regions")[1]["spots"] == [spot]
    for body in ({"name": "西山", "lat": 25, "lon": 102}, {"name": "", "lat": 25, "lon": 102},
                 {"name": "远", "lat": 95, "lon": 102}, {"name": "缺"}):
        with pytest.raises(HTTPError) as e:
            call(base, "/api/spots", "POST", body, origin=base)
        assert e.value.code == 400
    assert call(base, f"/api/spots/{spot['id']}", "DELETE", origin=base)[1] == {"deleted": spot["id"]}
    assert call(base, "/api/regions")[1]["spots"] == []
    with pytest.raises(HTTPError) as e:
        call(base, f"/api/spots/{spot['id']}", "DELETE", origin=base)
    assert e.value.code == 404


def test_state_changes_reject_foreign_host_even_when_origin_matches(api):
    base, _, _ = api
    # DNS rebinding: the page and the Host header both carry the attacker's name
    req = Request(base + "/api/spots", method="POST", data=json.dumps({"name": "x", "lat": 1, "lon": 1}).encode())
    req.add_header("Host", "evil.example:8765")
    req.add_header("Origin", "http://evil.example:8765")
    with pytest.raises(HTTPError) as e:
        urlopen(req)
    assert e.value.code == 403
