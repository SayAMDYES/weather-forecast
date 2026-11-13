import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from glow import archive, pipeline
from glow.config import Box, compute_box_for, load_config
from glow.detail import Context, point_detail
from glow.regions import Region
from glow.pipeline import _merge_history, _time_weights, bracket, plan_day, score_event
from glow.sources.base import CloudField, RunInfo, steps_for
from glow.terrain import Terrain

CFG = load_config()
T0 = datetime(2026, 11, 5, 6, tzinfo=timezone.utc)
LATS = np.arange(32.0, 39.01, 0.25)
LONS = np.arange(133.0, 147.01, 0.25)


def field(hours, low=0.0, mid=0.0, high=0.0):
    shape = (len(LATS), len(LONS))
    return CloudField(T0 + timedelta(hours=hours), LATS, LONS,
                      np.full(shape, low), np.full(shape, mid), np.full(shape, high))


def flat_terrain():
    lats = np.arange(32.0, 39.0, 0.1)
    lons = np.arange(133.0, 147.0, 0.1)
    z = np.zeros((len(lats), len(lons)))
    return Terrain(lats, lons, z, z)


def test_time_weights_interpolate_linearly_between_3h_steps():
    target = np.array([(T0 + timedelta(hours=1)).timestamp(), (T0 + timedelta(hours=3)).timestamp()])
    w = _time_weights(target, [T0, T0 + timedelta(hours=3)])
    assert w[:, 0] == pytest.approx([2 / 3, 1 / 3])
    assert w[:, 1] == pytest.approx([0, 1])


def test_bracket_keeps_steps_around_window():
    fs = [field(h) for h in (0, 3, 6, 9)]
    got = bracket(fs, T0 + timedelta(hours=3, minutes=40), T0 + timedelta(hours=4, minutes=5))
    assert [f.valid_time for f in got] == [T0 + timedelta(hours=3), T0 + timedelta(hours=6)]


def test_merge_history_keeps_recent_events_with_their_runs(tmp_path):
    cfg = load_config()
    cfg["paths"]["output"] = tmp_path / "out"
    cfg["paths"]["archive"] = tmp_path / "out" / "archive"
    cfg["paths"]["spots"] = tmp_path / "spots.json"
    display = Box(35.0, 36.0, 139.0, 140.0, 0.02)
    region = Region("r", "测试", display, compute_box_for(display, 500), "Asia/Shanghai", "")
    out = tmp_path / "out" / "regions" / "r"
    (out / "grid").mkdir(parents=True)
    (out / "sun").mkdir()
    dates = ("2026-11-03", "2026-11-04", "2026-11-05")
    for d in dates:
        (out / "grid" / f"{d}_sunset_gfs.png").write_bytes(b"x")
        (out / "sun" / f"{d}_sunset.json").write_text("{}", encoding="utf-8")
    runs = {"gfs": {"init": "2026-11-05T00:00:00+00:00", "source": "test", "resolution": 0.25,
                    "tag": "0p25", "steps": [10]}}
    old = {"runs": runs, "events": {"sunset": [
        {"date": d, "files": {"gfs": f"regions/r/grid/{d}_sunset_gfs.png"},
         "sun": f"regions/r/sun/{d}_sunset.json"} for d in dates
    ]}}
    (out / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    events = {"sunset": [{"date": "2026-11-05", "files": {"gfs": "regions/r/grid/2026-11-05_sunset_gfs.png"}}]}
    keep = set()

    _merge_history(cfg, region, events, keep, datetime(2026, 11, 6, 12, tzinfo=timezone.utc))

    assert [e["date"] for e in events["sunset"]] == ["2026-11-04", "2026-11-05"]
    assert events["sunset"][0]["runs"] == runs
    assert "grid/2026-11-04_sunset_gfs.png" in keep


def test_plan_day_skips_events_that_have_passed():
    display = Box(25.0, 25.4, 102.5, 102.9, 0.2)
    region = Region("r", "测试", display, compute_box_for(display, 500), "Asia/Shanghai", "")
    planned = plan_day(CFG, region, datetime(2026, 11, 6).date(),
                       now=datetime(2026, 11, 6, 4, tzinfo=timezone.utc))
    assert [event for event, _, _ in planned] == ["sunset"]


def test_run_region_writes_permanent_archive(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["paths"]["output"] = tmp_path / "out"
    cfg["paths"]["archive"] = tmp_path / "out" / "archive"
    cfg["paths"]["spots"] = tmp_path / "spots.json"
    cfg["paths"]["cache"] = tmp_path / "cache"
    display = Box(25.0, 25.4, 102.5, 102.9, 0.2)
    region = Region("run-test", "测试", display, compute_box_for(display, 500), "Asia/Shanghai",
                    "2026-11-01T00:00:00+00:00")
    lats = np.arange(20.0, 51.0, 0.5)
    lons = np.arange(110.0, 171.0, 0.5)
    shape = (len(lats), len(lons))
    terrain = Terrain(lats, lons, np.zeros(shape), np.zeros(shape))
    init = datetime(2026, 11, 6, tzinfo=timezone.utc)
    times = [init + timedelta(hours=h) for h in range(-12, 121, 3)]

    def fake_fetch(cfg_, model, region_, windows, now, progress):
        fields = [CloudField(t, lats, lons, np.full(shape, 0.4), np.full(shape, 0.5), np.full(shape, 0.2))
                  for t in times]
        return RunInfo(model, init, "test", 0.25, "0p25"), list(range(-12, 121, 3)), fields

    monkeypatch.setattr(pipeline, "fetch_model", fake_fetch)
    monkeypatch.setattr(pipeline.terrain, "load_box", lambda cfg_, box, progress=lambda msg: None: terrain)
    manifest = pipeline.run_region(cfg, region, now=datetime(2026, 11, 6, 6, tzinfo=timezone.utc))

    snapshots = archive.list_archives(cfg)[0]["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["point_details"] is True
    assert snapshots[0]["date_min"] <= manifest["events"]["sunset"][0]["date"] <= snapshots[0]["date_max"]
    ctx = archive.load_point_context(cfg, region.id, snapshots[0]["snapshot"], "sunset",
                                     manifest["events"]["sunset"][0]["date"])
    assert set(ctx.fields) == {"gfs", "ecmwf"}
    assert ctx.terrain.mean_m.shape == shape


def test_run_region_day_replaces_only_selected_date(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["paths"]["output"] = tmp_path / "out"
    cfg["paths"]["archive"] = tmp_path / "out" / "archive"
    cfg["paths"]["spots"] = tmp_path / "spots.json"
    cfg["paths"]["cache"] = tmp_path / "cache"
    display = Box(25.0, 25.4, 102.5, 102.9, 0.2)
    region = Region("day-test", "测试", display, compute_box_for(display, 500), "Asia/Shanghai",
                    "2026-11-01T00:00:00+00:00")
    out = tmp_path / "out" / "regions" / region.id
    (out / "grid").mkdir(parents=True)
    (out / "sun").mkdir()

    old_runs = {
        "gfs": {"init": "2026-11-05T00:00:00+00:00", "source": "old-gfs", "resolution": 0.25,
                "tag": "0p25", "steps": [10, 11]},
        "ecmwf": {"init": "2026-11-04T18:00:00+00:00", "source": "old-ecmwf", "resolution": 0.1,
                  "tag": "0p1", "steps": [15, 18]},
    }
    old_events = []
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        files = {}
        for model in ("gfs", "ecmwf"):
            rel = f"grid/{day}_sunset_{model}.png"
            (out / rel).write_bytes(b"old")
            files[model] = f"regions/{region.id}/{rel}"
        (out / "sun" / f"{day}_sunset.json").write_text("{}", encoding="utf-8")
        old_events.append({
            "date": day,
            "files": files,
            "sun": f"regions/{region.id}/sun/{day}_sunset.json",
        })
    old_manifest = {
        "region": region.to_json(),
        "generated_at": "2026-11-06T00:00:00+00:00",
        "elapsed_s": 1.0,
        "timezone": region.timezone,
        "runs": old_runs,
        "models": ["gfs", "ecmwf"],
        "grid": {"lat_max": display.lat_max, "lat_min": display.lat_min, "lon_min": display.lon_min,
                 "lon_max": display.lon_max, "step": display.step, "nx": 3, "ny": 3,
                 "layers": ["index", "low", "mid", "high"]},
        "spots": [],
        "events": {"sunrise": [], "sunset": old_events},
    }
    (out / "manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")

    lats = np.arange(20.0, 51.0, 0.5)
    lons = np.arange(110.0, 171.0, 0.5)
    shape = (len(lats), len(lons))
    terrain = Terrain(lats, lons, np.zeros(shape), np.zeros(shape))
    new_init = datetime(2026, 11, 6, tzinfo=timezone.utc)
    times = [new_init + timedelta(hours=h) for h in range(-12, 121, 3)]

    def fake_fetch(cfg_, model, region_, windows, now, progress):
        assert len(windows) == 2
        fields = [CloudField(t, lats, lons, np.full(shape, 0.4), np.full(shape, 0.5), np.full(shape, 0.2))
                  for t in times]
        return RunInfo(model, new_init, "new", 0.25, "0p25"), list(range(-12, 121, 3)), fields

    monkeypatch.setattr(pipeline, "fetch_model", fake_fetch)
    monkeypatch.setattr(pipeline.terrain, "load_box", lambda cfg_, box, progress=lambda msg: None: terrain)
    monkeypatch.setattr(pipeline, "_archive_existing_result", lambda *args: None)
    monkeypatch.setattr(pipeline.archive, "write_snapshot", lambda *args, **kwargs: None)

    manifest = pipeline.run_region_day(
        cfg, region, "2026-11-07", now=datetime(2026, 11, 6, 6, tzinfo=timezone.utc)
    )

    new_runs = {
        model: {"init": new_init.isoformat(), "source": "new", "resolution": 0.25,
                "tag": "0p25", "steps": list(range(-12, 121, 3))}
        for model in ("gfs", "ecmwf")
    }
    for day in ("2026-11-05", "2026-11-06"):
        entry = next(e for e in manifest["events"]["sunset"] if e["date"] == day)
        old_entry = next(e for e in old_events if e["date"] == day)
        assert entry["files"] == old_entry["files"]
        assert entry["runs"] == old_runs
    for event in ("sunrise", "sunset"):
        entry = next(e for e in manifest["events"][event] if e["date"] == "2026-11-07")
        assert entry["runs"] == new_runs
        assert set(entry["files"]) == {"gfs", "ecmwf"}
        for rel in entry["files"].values():
            assert (tmp_path / "out" / rel).exists()
    assert manifest["runs"] == new_runs
    assert manifest["generated_at"] == "2026-11-06T06:00:00+00:00"

    manifest = pipeline.run_region_day(
        cfg, region, "2026-11-09", now=datetime(2026, 11, 6, 6, tzinfo=timezone.utc)
    )
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        entry = next(e for e in manifest["events"]["sunset"] if e["date"] == day)
        assert entry["runs"] == (old_runs if day != "2026-11-07" else new_runs)
    for event in ("sunrise", "sunset"):
        entry = next(e for e in manifest["events"][event] if e["date"] == "2026-11-09")
        assert entry["runs"] == new_runs


def test_steps_for_brackets_each_window():
    w = [(T0 + timedelta(hours=1, minutes=35), T0 + timedelta(hours=2, minutes=5))]
    assert steps_for(T0, w, 3, 144) == [0, 3]
    assert steps_for(T0, w, 1, 120) == [1, 2, 3]
    assert steps_for(T0 + timedelta(hours=6), w, 3, 144) is None


def test_mid_cloud_change_over_time_is_interpolated():
    params = CFG["scoring"]
    fs = [field(0, mid=0.0), field(3, mid=0.6)]
    lat, lon = np.array([35.4]), np.array([139.6])
    early = score_event(fs, lat, lon, np.array([T0.timestamp()]), np.array([250.0]), params, flat_terrain())
    later = score_event(fs, lat, lon, np.array([(T0 + timedelta(hours=1.5)).timestamp()]), np.array([250.0]),
                        params, flat_terrain())
    assert early["local"].mid[0] == pytest.approx(0.0)
    assert later["local"].mid[0] == pytest.approx(0.3)


def test_point_detail_structure():
    ctx = Context.__new__(Context)
    ctx.cfg = CFG
    display = Box(35.0, 36.0, 139.0, 140.0, 0.02)
    ctx.region = Region("t", "测试", display, compute_box_for(display, 500), "Asia/Tokyo", "")
    ctx.manifest = {}
    ctx.fields = {"gfs": [field(h, mid=0.5) for h in range(0, 4)], "ecmwf": [field(h, mid=0.2) for h in (0, 3)]}
    ctx.terrain = flat_terrain()
    d = point_detail(ctx, 35.444, 139.638, "sunset", "2026-11-05")
    assert set(d["models"]) == {"gfs", "ecmwf"}
    n_alpha = len(CFG["scoring"]["alphas_deg"])
    for m in d["models"].values():
        assert len(m["rays"]["mid"]) == n_alpha and len(m["rays"]["high"]) == n_alpha
        assert len(m["profile"]["low"]) == len(d["geometry"]["s_km"])
        assert m["mid"] > 0
    assert d["spread"] == pytest.approx(abs(d["models"]["gfs"]["score"] - d["models"]["ecmwf"]["score"]), abs=0.11)
    assert 240 < d["azimuth"] < 260


def test_point_detail_refuses_points_outside_region():
    ctx = Context.__new__(Context)
    ctx.cfg = CFG
    display = Box(35.0, 36.0, 139.0, 140.0, 0.02)
    ctx.region = Region("t", "测试", display, compute_box_for(display, 500), "Asia/Tokyo", "")
    with pytest.raises(ValueError):
        point_detail(ctx, 38.27, 140.87, "sunset", "2026-11-05")
