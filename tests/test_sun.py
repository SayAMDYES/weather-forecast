import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from glow.config import Box
from glow.sun import event_detail, event_grid, event_time

TZ = "Asia/Tokyo"
JST = timezone(timedelta(hours=9))
NOAA = json.loads((Path(__file__).parent / "data" / "noaa_sun.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", NOAA, ids=lambda c: f"{c['name']}-{c['date']}-{c['event']}")
def test_event_time_matches_noaa(case):
    d = date.fromisoformat(case["date"])
    got = event_time(case["lat"], case["lon"], d, case["event"], TZ)
    expected = datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(minutes=case["jst_min"])
    assert abs((got - expected).total_seconds()) < 60


@pytest.mark.parametrize("case", NOAA[:8], ids=lambda c: f"{c['name']}-{c['date']}-{c['event']}")
def test_azimuth_matches_noaa(case):
    d = date.fromisoformat(case["date"])
    _, _, az = event_detail(case["lat"], case["lon"], d, case["event"], 0, TZ)
    assert az == pytest.approx(case["azimuth"], abs=0.5)


def test_grid_interpolation_matches_point_values():
    box = Box(34.4, 37.0, 137.8, 141.2, 0.02)
    d = date(2026, 12, 22)
    g = event_grid(box, d, "sunset", 10, TZ)
    lats, lons = box.grid()
    for j, i in [(0, 0), (37, 91), (130, 170), (64, 3)]:
        exact = event_time(lats[j], lons[i], d, "sunset", TZ).timestamp()
        assert abs(g["event"][j, i] - exact) < 1.0
        assert g["target"][j, i] - g["event"][j, i] == pytest.approx(600)
    assert np.all((g["azimuth"] > 235) & (g["azimuth"] < 250))
