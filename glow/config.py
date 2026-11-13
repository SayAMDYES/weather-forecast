import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"
# Personal overrides, not tracked by git; only applied on top of the default file
LOCAL_CONFIG = ROOT / "config" / "local.yaml"

# Beyond the light path itself, so bilinear lookups at the far end still have neighbours
PATH_MARGIN_KM = 50
OBLIQUITY_DEG = 23.44
# Refraction and the ±10 min glow offset move the azimuth a little past the geometric solstice value
AZIMUTH_MARGIN_DEG = 3


@dataclass(frozen=True)
class Box:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    step: float = 0.0

    def grid(self):
        """Regular grid with rows ordered north to south, matching image row order."""
        ny = int(round((self.lat_max - self.lat_min) / self.step)) + 1
        nx = int(round((self.lon_max - self.lon_min) / self.step)) + 1
        lats = self.lat_max - np.arange(ny) * self.step
        lons = self.lon_min + np.arange(nx) * self.step
        return lats, lons

    def contains(self, lat, lon):
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


def max_azimuth_from_ew(abs_lat):
    """Largest angle between the rising/setting sun and due east/west over the year at this latitude."""
    s = math.sin(math.radians(OBLIQUITY_DEG)) / math.cos(math.radians(abs_lat))
    return min(90.0, math.degrees(math.asin(min(1.0, s))) + AZIMUTH_MARGIN_DEG)


def compute_box_for(display, max_km):
    """Display box grown by the light-path reach: full length east/west, the azimuth share north/south."""
    reach = max_km + PATH_MARGIN_KM
    abs_lat = max(abs(display.lat_min), abs(display.lat_max))
    dlat = reach * math.sin(math.radians(max_azimuth_from_ew(abs_lat))) / 111.0
    lat_far = min(abs_lat + dlat, 80.0)
    dlon = reach / (111.0 * math.cos(math.radians(lat_far)))
    return Box(display.lat_min - dlat, display.lat_max + dlat, display.lon_min - dlon, display.lon_max + dlon)


def _merge(base, override):
    """Nested mappings merge key by key; any other value, lists included, replaces the default outright."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if Path(path) == DEFAULT_CONFIG and LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, encoding="utf-8") as f:
            _merge(cfg, yaml.safe_load(f) or {})
    for key in ("cache", "output", "archive", "regions", "spots"):
        cfg["paths"][key] = (ROOT / cfg["paths"][key]).resolve()
    if host := os.getenv("GLOW_SERVER_HOST"):
        cfg["server"]["host"] = host
    if port := os.getenv("GLOW_SERVER_PORT"):
        cfg["server"]["port"] = int(port)
    return cfg
