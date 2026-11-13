"""Copernicus GLO-90 DEM reduced to a 0.01° (~1 km) grid, cached per 1° tile.

Only the tile's built-in overview level is read over HTTP (factor 2 ≈ 180 m), which is a quarter of the
download of the full tile. Two reductions are kept per cell: the maximum, so ridges still block grazing
light after downsampling, and the mean, used as the observer's altitude.
"""
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import rasterio

from glow.sources import http

log = logging.getLogger(__name__)

PER_DEG = 100
STEP = 1.0 / PER_DEG
_GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
             "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "2"}


@dataclass
class Terrain:
    lats: np.ndarray
    lons: np.ndarray
    max_m: np.ndarray
    mean_m: np.ndarray


def _tile_name(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_30_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _tile(cfg, lat, lon):
    """(max, mean) arrays of shape (100, 100), rows ascending in latitude; None for open sea."""
    name = _tile_name(lat, lon)
    cache_dir = cfg["paths"]["cache"] / "dem"
    path = cache_dir / f"{name}.npz"
    missing = cache_dir / f"{name}.missing"
    if path.exists():
        with np.load(path) as z:
            return z["max_m"].astype(np.float32), z["mean_m"].astype(np.float32)
    if missing.exists():
        return None
    url = cfg["terrain"]["tile_url"].format(n=name)
    status = http.status(url)
    if status == 404:
        # Open-ocean tiles are simply not published
        cache_dir.mkdir(parents=True, exist_ok=True)
        missing.touch()
        return None
    if status != 200:
        raise RuntimeError(f"DEM tile {name} unavailable (HTTP {status})")
    factor = cfg["terrain"]["overview_factor"]
    with rasterio.Env(**_GDAL_ENV), rasterio.open("/vsicurl/" + url) as ds:
        h, w = ds.height // factor, ds.width // factor
        a = ds.read(1, out_shape=(h, w)).astype(np.float32)
    a = np.where(np.isfinite(a), np.maximum(a, 0), 0)[::-1]
    blocks = a.reshape(PER_DEG, h // PER_DEG, PER_DEG, w // PER_DEG)
    mx, mn = blocks.max(axis=(1, 3)), blocks.mean(axis=(1, 3))
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f"{name}.part.npz"
    np.savez_compressed(tmp, max_m=np.rint(mx).astype(np.int16), mean_m=np.rint(mn).astype(np.int16))
    tmp.replace(path)
    return mx, mn


def load_box(cfg, box, progress=lambda msg, frac=None: None):
    """Terrain on the 0.01° grid covering the box (tile-aligned), downloading tiles not cached yet."""
    lat0, lat1 = math.floor(box.lat_min), math.ceil(box.lat_max)
    lon0, lon1 = math.floor(box.lon_min), math.ceil(box.lon_max)
    tiles = [(la, lo) for la in range(lat0, lat1) for lo in range(lon0, lon1)]
    done = [0]

    def fetch(t):
        out = _tile(cfg, *t)
        done[0] += 1
        progress(f"地形 {done[0]}/{len(tiles)}", done[0] / len(tiles))
        return out

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch, tiles))
    max_m = np.zeros(((lat1 - lat0) * PER_DEG, (lon1 - lon0) * PER_DEG), dtype=np.float32)
    mean_m = np.zeros_like(max_m)
    for (la, lo), r in zip(tiles, results):
        if r is None:
            continue
        j, i = (la - lat0) * PER_DEG, (lo - lon0) * PER_DEG
        max_m[j:j + PER_DEG, i:i + PER_DEG], mean_m[j:j + PER_DEG, i:i + PER_DEG] = r
    lats = lat0 + (np.arange(max_m.shape[0]) + 0.5) * STEP
    lons = lon0 + (np.arange(max_m.shape[1]) + 0.5) * STEP
    return Terrain(lats, lons, max_m, mean_m)
