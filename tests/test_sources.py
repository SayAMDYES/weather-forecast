from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from glow.config import Box, compute_box_for, load_config
from glow.regions import Region
from glow.sources import ecmwf, gfs, openmeteo
from glow.sources.base import RunInfo
from glow.sources.grib import decode, split_messages

SAMPLE = (Path(__file__).parent / "data" / "gfs_nomads_f024_subset.grib2").read_bytes()


def test_grib_sample_decodes_to_instant_layers():
    assert len(split_messages(SAMPLE)) == 6
    fields = decode(SAMPLE)
    layers = gfs._to_layers(fields, Box(33.0, 38.0, 134.5, 146.0))
    for k in ("low", "mid", "high"):
        assert layers[k].shape == (len(layers["lats"]), len(layers["lons"]))
        assert 0 <= layers[k].min() and layers[k].max() <= 1
    assert np.all(np.diff(layers["lats"]) > 0) and np.all(np.diff(layers["lons"]) > 0)
    # The instant record is used, not the 6-hour average that NOMADS also returns
    lcc = next(f for f in fields if f["shortName"] == "lcc")
    assert np.allclose(layers["low"], lcc["values"] / 100.0)


def test_ecmwf_merges_adjacent_ranges():
    assert ecmwf._merge_ranges([(0, 10), (10, 5), (40, 2)]) == [[0, 14], [40, 41]]


def test_openmeteo_response_lands_on_grid(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["paths"]["cache"] = tmp_path
    cfg["sources"]["openmeteo"]["request_interval_s"] = 0
    cfg["sources"]["openmeteo"]["points_per_request"] = 500
    init = datetime(2026, 11, 3, tzinfo=timezone.utc)
    run = RunInfo("gfs", init, "open-meteo", 0.25, "openmeteo")
    times = [init + timedelta(hours=h) for h in (31, 32)]

    class Resp:
        ok = True
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, **kw):
        lats = [float(x) for x in params["latitude"].split(",")]
        lons = [float(x) for x in params["longitude"].split(",")]
        # cover encodes the location so placement can be checked: low = lat, mid = lon - 100
        return Resp([{"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
                                 "cloud_cover_low": [la, la], "cloud_cover_mid": [lo - 100, lo - 100],
                                 # a single missing value becomes 0
                                 "cloud_cover_high": [None if (la, lo) == (35.0, 140.0) else 10, 50]}}
                     for la, lo in zip(lats, lons)])

    monkeypatch.setattr(openmeteo.http, "get", fake_get)
    display = Box(34.5, 36.5, 139.0, 141.0, 0.02)
    region = Region("t", "测试", display, compute_box_for(display, 500), "Asia/Tokyo", "")
    fs = openmeteo.load_steps(cfg, run, [31, 32], region)
    f = fs[0]
    assert f.valid_time == times[0]
    j = int(np.argmin(np.abs(f.lats - 35.0)))
    i = int(np.argmin(np.abs(f.lons - 140.0)))
    assert f.low[j, i] == np.float32(0.35)
    assert f.mid[j, i] == np.float32(0.40)
    assert f.high[j, i] == 0 and fs[1].high[j, i] == np.float32(0.5)
