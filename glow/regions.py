"""User-drawn forecast regions, persisted as JSON."""
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from glow.config import Box, compute_box_for

_lock = threading.Lock()


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    display: Box
    compute: Box
    timezone: str
    created_at: str

    def to_json(self):
        d = self.display
        return {"id": self.id, "name": self.name, "timezone": self.timezone, "created_at": self.created_at,
                "display": {"lat_min": d.lat_min, "lat_max": d.lat_max, "lon_min": d.lon_min,
                            "lon_max": d.lon_max, "step": d.step}}


@lru_cache(maxsize=1)
def _tz_finder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder()


def timezone_at(lat, lon):
    """IANA zone at the point; open sea falls back to the nautical Etc/GMT zone, never None."""
    tz = _tz_finder().timezone_at(lat=lat, lng=lon)
    if tz:
        return tz
    hours = round(lon / 15)
    # Etc/GMT names use the inverted POSIX sign: Etc/GMT-9 is UTC+9
    return "Etc/GMT" if hours == 0 else f"Etc/GMT{-hours:+d}"


def frame_box(cfg, lat, lon):
    """Fixed-size display box centred on the point (centre snapped to the grid), or ValueError for the user."""
    rcfg = cfg["regions"]
    f = rcfg["frame"]
    step = f["step"]
    lat_c, lon_c = round(lat / step) * step, round(lon / step) * step
    box = Box(round(lat_c - f["lat_span"] / 2, 6), round(lat_c + f["lat_span"] / 2, 6),
              round(lon_c - f["lon_span"] / 2, 6), round(lon_c + f["lon_span"] / 2, 6), step)
    if max(abs(box.lat_min), abs(box.lat_max)) > rcfg["max_abs_lat"]:
        raise ValueError(f"框的南北边界要在南北纬 {rcfg['max_abs_lat']}° 以内（更高纬度太阳可能不升不落）")
    if box.lon_min < -180 or box.lon_max > 180:
        raise ValueError("区域不能跨越 180° 经线")
    return box


def _from_json(cfg, d):
    display = Box(**d["display"])
    return Region(d["id"], d["name"], display, compute_box_for(display, cfg["scoring"]["ray"]["max_km"]),
                  d["timezone"], d["created_at"])


def load_all(cfg):
    path = cfg["paths"]["regions"]
    if not path.exists():
        return []
    return [_from_json(cfg, d) for d in json.loads(path.read_text(encoding="utf-8"))]


def get(cfg, region_id):
    return next((r for r in load_all(cfg) if r.id == region_id), None)


def _save(cfg, regions):
    path = cfg["paths"]["regions"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([r.to_json() for r in regions], ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def create(cfg, name, lat, lon):
    display = frame_box(cfg, lat, lon)
    name = (name or "").strip()[:20] or "未命名区域"
    tz = timezone_at((display.lat_min + display.lat_max) / 2, (display.lon_min + display.lon_max) / 2)
    region = Region(uuid.uuid4().hex[:8], name, display, compute_box_for(display, cfg["scoring"]["ray"]["max_km"]),
                    tz, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with _lock:
        _save(cfg, load_all(cfg) + [region])
    return region


def delete(cfg, region_id):
    with _lock:
        regions = load_all(cfg)
        kept = [r for r in regions if r.id != region_id]
        if len(kept) == len(regions):
            return False
        _save(cfg, kept)
    return True
