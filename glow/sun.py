from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from astral import Observer
from astral.sun import azimuth, sunrise, sunset

from glow.geometry import bilinear

_EVENTS = {"sunset": sunset, "sunrise": sunrise}

# Sun times vary almost linearly over a few tenths of a degree, so a coarse astral grid
# interpolated to the display grid stays well under a second from per-point astral values.
COARSE_STEP = 0.1


def event_time(lat, lon, local_date, event, tz):
    """UTC datetime of sunrise/sunset (sun centre at -0.833°, NOAA convention) on the given local date."""
    return _EVENTS[event](Observer(lat, lon), date=local_date, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def event_detail(lat, lon, local_date, event, offset_min, tz):
    """Event time, target time (event + offset) and solar azimuth at the target time."""
    t_event = event_time(lat, lon, local_date, event, tz)
    t_target = t_event + timedelta(minutes=offset_min)
    return t_event, t_target, azimuth(Observer(lat, lon), t_target)


def event_grid(box, local_date, event, offset_min, tz):
    """Event and target epoch seconds plus target azimuth on the display grid (rows north→south)."""
    c_lats = np.arange(np.floor(box.lat_min / COARSE_STEP) - 1, np.ceil(box.lat_max / COARSE_STEP) + 2) * COARSE_STEP
    c_lons = np.arange(np.floor(box.lon_min / COARSE_STEP) - 1, np.ceil(box.lon_max / COARSE_STEP) + 2) * COARSE_STEP
    t_event = np.empty((len(c_lats), len(c_lons)))
    az = np.empty_like(t_event)
    for j, la in enumerate(c_lats):
        for i, lo in enumerate(c_lons):
            te, tt, a = event_detail(la, lo, local_date, event, offset_min, tz)
            t_event[j, i] = te.timestamp()
            az[j, i] = a
    lats, lons = box.grid()
    lat_q, lon_q = np.meshgrid(lats, lons, indexing="ij")
    ev = bilinear(t_event, c_lats, c_lons, lat_q, lon_q)
    return {
        "event": ev,
        "target": ev + offset_min * 60.0,
        "azimuth": bilinear(az, c_lats, c_lons, lat_q, lon_q),
        "coarse": {"lats": c_lats, "lons": c_lons, "event": t_event, "azimuth": az},
    }
