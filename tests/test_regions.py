import numpy as np
import pytest

from glow.config import Box, compute_box_for, load_config, max_azimuth_from_ew
from glow.geometry import destination
from glow.regions import frame_box, timezone_at

CFG = load_config()


def test_frame_is_fixed_size_and_snapped():
    f = CFG["regions"]["frame"]
    b = frame_box(CFG, 38.2671, 140.8694)
    assert (b.lat_max - b.lat_min, b.lon_max - b.lon_min, b.step) == (f["lat_span"], f["lon_span"], f["step"])
    assert (b.lat_min, b.lat_max, b.lon_min, b.lon_max) == (36.26, 40.26, 138.36, 143.36)
    lats, lons = b.grid()
    assert lats[0] == b.lat_max and lats[-1] == pytest.approx(b.lat_min) and lons[-1] == pytest.approx(b.lon_max)
    assert len(lats) * len(lons) == 201 * 251


@pytest.mark.parametrize("center,msg", [((59.0, 10.0), "纬"), ((-59.0, 10.0), "纬"), ((35.0, 179.0), "180")])
def test_frame_rejects(center, msg):
    with pytest.raises(ValueError, match=msg):
        frame_box(CFG, *center)


def test_timezone_lookup_and_ocean_fallback():
    assert timezone_at(38.27, 140.87) == "Asia/Tokyo"
    assert timezone_at(48.85, 2.35) == "Europe/Paris"
    # Open Pacific east of Japan: Etc/GMT-9 means UTC+9
    tz = timezone_at(30.0, 140.0)
    assert tz in ("Etc/GMT-9", "Asia/Tokyo")


@pytest.mark.parametrize("display", [Box(34.4, 37.0, 137.8, 141.2), Box(41.3, 45.6, 139.7, 145.9),
                                     Box(52.0, 58.0, -5.0, 1.0), Box(-38.0, -33.0, 140.0, 146.0)])
def test_compute_box_holds_every_light_path(display):
    """Every display point's 500 km path toward any rising/setting azimuth stays inside the compute box."""
    comp = compute_box_for(display, CFG["scoring"]["ray"]["max_km"])
    dev = max_azimuth_from_ew(max(abs(display.lat_min), abs(display.lat_max))) - 3
    lat = np.array([display.lat_min, display.lat_min, display.lat_max, display.lat_max])
    lon = np.array([display.lon_min, display.lon_max, display.lon_min, display.lon_max])
    for az in np.concatenate([np.linspace(90 - dev, 90 + dev, 9), np.linspace(270 - dev, 270 + dev, 9)]):
        la, lo = destination(lat, lon, az, CFG["scoring"]["ray"]["max_km"])
        assert np.all((la >= comp.lat_min) & (la <= comp.lat_max) & (lo >= comp.lon_min) & (lo <= comp.lon_max))
