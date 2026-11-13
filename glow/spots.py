"""Favourite viewing spots, added and removed from the web page and shared by all regions."""
import json
import math
import os
import threading
import uuid

MAX_NAME = 20

_lock = threading.Lock()


def load_all(cfg):
    path = cfg["paths"]["spots"]
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def inside(cfg, box):
    return [s for s in load_all(cfg) if box.contains(s["lat"], s["lon"])]


def _save(cfg, spots):
    path = cfg["paths"]["spots"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(spots, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def create(cfg, name, lat, lon):
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        raise ValueError("请给机位起个名字")
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("坐标超出范围")
    with _lock:
        spots = load_all(cfg)
        # The overview table and map labels identify spots by name
        if any(s["name"] == name for s in spots):
            raise ValueError(f"已有名为「{name}」的机位")
        spot = {"id": uuid.uuid4().hex[:8], "name": name, "lat": round(lat, 4), "lon": round(lon, 4)}
        _save(cfg, spots + [spot])
    return spot


def delete(cfg, spot_id):
    with _lock:
        spots = load_all(cfg)
        kept = [s for s in spots if s["id"] != spot_id]
        if len(kept) == len(spots):
            return False
        _save(cfg, kept)
    return True
