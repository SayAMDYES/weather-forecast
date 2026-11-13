from datetime import datetime, timedelta, timezone

import numpy as np

from glow import archive
from glow.config import Box, compute_box_for, load_config
from glow.detail import point_detail
from glow.regions import Region
from glow.sources.base import CloudField
from glow.terrain import Terrain


def fixture(tmp_path):
    cfg = load_config()
    cfg["paths"]["output"] = tmp_path / "out"
    cfg["paths"]["archive"] = tmp_path / "out" / "archive"
    cfg["paths"]["spots"] = tmp_path / "spots.json"
    display = Box(35.0, 36.0, 139.0, 140.0, 0.02)
    region = Region("test-region", "测试", display, compute_box_for(display, 500), "Asia/Tokyo",
                    "2026-11-01T00:00:00+00:00")
    out = cfg["paths"]["output"] / "regions" / region.id
    grid = out / "grid" / "2026-11-05_sunset_gfs.png"
    sun = out / "sun" / "2026-11-05_sunset.json"
    grid.parent.mkdir(parents=True)
    sun.parent.mkdir(parents=True)
    grid.write_bytes(b"png")
    sun.write_text("{}", encoding="utf-8")
    entry = {
        "date": "2026-11-05",
        "files": {"gfs": "regions/test-region/grid/2026-11-05_sunset_gfs.png"},
        "sun": "regions/test-region/sun/2026-11-05_sunset.json",
    }
    manifest = {
        "region": region.to_json(),
        "generated_at": "2026-11-05T04:15:27+00:00",
        "timezone": "Asia/Tokyo",
        "runs": {"gfs": {"init": "2026-11-05T00:00:00+00:00", "source": "test",
                         "resolution": 0.25, "tag": "0p25", "steps": [8, 10]}},
        "models": ["gfs"],
        "grid": {"lat_min": 35.0, "lat_max": 36.0, "lon_min": 139.0, "lon_max": 140.0,
                 "step": 0.02, "nx": 51, "ny": 51, "layers": ["index", "low", "mid", "high"]},
        "spots": [],
        "events": {"sunrise": [], "sunset": [entry]},
    }
    lats = np.arange(20.0, 51.0)
    lons = np.arange(110.0, 171.0)
    shape = (len(lats), len(lons))
    fields = [
        CloudField(datetime(2026, 11, 5, hour, tzinfo=timezone.utc), lats, lons,
                   np.zeros(shape), np.zeros(shape), np.zeros(shape))
        for hour in (8, 10)
    ]
    terrain = Terrain(lats, lons, np.zeros(shape), np.zeros(shape))
    events = [{"event": "sunset", "date": "2026-11-05", "entry": entry, "fields": {"gfs": fields}}]
    return cfg, region, manifest, events, terrain


def test_snapshot_survives_active_output_deletion_and_keeps_point_context(tmp_path):
    cfg, region, manifest, events, terrain = fixture(tmp_path)
    snapshot = archive.write_snapshot(cfg, region, manifest, events, terrain,
                                      archived_at=datetime(2026, 11, 6, tzinfo=timezone.utc))

    assert snapshot["point_details"] is True
    assert snapshot["runs"]["gfs"][0]["init"] == "2026-11-05T00:00:00+00:00"
    assert [r["id"] for r in archive.list_archives(cfg)] == [region.id]
    snapshot_manifest = archive.load_snapshot_manifest(cfg, region.id, snapshot["snapshot"])
    assert snapshot_manifest["scoring"] == cfg["scoring"]
    assert snapshot_manifest["events"]["sunset"][0]["detail_fields"]["gfs"].endswith(".npz")

    ctx = archive.load_point_context(cfg, region.id, snapshot["snapshot"], "sunset", "2026-11-05")
    detail = point_detail(ctx, 35.5, 139.5, "sunset", "2026-11-05")
    assert set(detail["models"]) == {"gfs"}

    active_grid = cfg["paths"]["output"] / events[0]["entry"]["files"]["gfs"]
    active_grid.unlink()
    archived_grid = cfg["paths"]["output"] / snapshot_manifest["events"]["sunset"][0]["files"]["gfs"]
    assert archived_grid.exists()


def test_snapshot_ids_are_unique_and_deletion_is_manual(tmp_path):
    cfg, region, manifest, events, terrain = fixture(tmp_path)
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    first = archive.write_snapshot(cfg, region, manifest, events, terrain, archived_at=at)
    second = archive.write_snapshot(cfg, region, manifest, events, terrain, archived_at=at)
    assert first["snapshot"] != second["snapshot"]

    assert archive.delete_snapshot(cfg, region.id, first["snapshot"]) is True
    remaining = archive.list_archives(cfg)[0]["snapshots"]
    assert [s["snapshot"] for s in remaining] == [second["snapshot"]]

    assert archive.delete_region(cfg, region.id) is True
    assert archive.list_archives(cfg) == []
    assert not archive.region_dir(cfg, region.id).exists()


def test_invalid_snapshot_ids_cannot_escape_archive(tmp_path):
    cfg, _, _, _, _ = fixture(tmp_path)
    assert archive.delete_snapshot(cfg, "test-region", "../index") is False
    assert archive.delete_region(cfg, "../out") is False
