"""Fallback: Open-Meteo forecast API (free for non-commercial use) sampled on a 0.25° grid."""
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from glow.sources import http
from glow.sources.base import RunInfo, cache_path, load_cached, save_step, steps_for

log = logging.getLogger(__name__)

# model -> (API model name, metadata name)
MODELS = {"gfs": ("gfs_global", "ncep_gfs025"), "ecmwf": ("ecmwf_ifs025", "ecmwf_ifs025")}
VARS = {"cloud_cover_low": "low", "cloud_cover_mid": "mid", "cloud_cover_high": "high"}
MAX_STEP = 120


def _meta(cfg, model):
    src = cfg["sources"]["openmeteo"]
    resp = http.get(f"{src['meta_url']}/{MODELS[model][1]}/static/meta.json", retries=src["retries"])
    resp.raise_for_status()
    return resp.json()


def find_run(cfg, model, windows, now=None):
    meta = _meta(cfg, model)
    init = datetime.fromtimestamp(meta["last_run_initialisation_time"], timezone.utc)
    step_h = max(1, meta.get("temporal_resolution_seconds", 3600) // 3600)
    steps = steps_for(init, windows, step_h, MAX_STEP)
    if steps is None:
        raise RuntimeError(f"Open-Meteo {model} run {init:%Y-%m-%d %HZ} does not cover the requested times")
    return RunInfo(model, init, "open-meteo", cfg["sources"]["openmeteo"]["resolution"], "openmeteo"), steps


def _grid(box, res):
    lats = np.arange(np.floor(box.lat_min / res) - 1, np.ceil(box.lat_max / res) + 2) * res
    lons = np.arange(np.floor(box.lon_min / res) - 1, np.ceil(box.lon_max / res) + 2) * res
    return lats, lons


def load_steps(cfg, run, steps, region, progress=lambda msg, frac=None: None):
    if all(cache_path(cfg, run, s, region.id).exists() for s in steps):
        return load_cached(cfg, run, steps, region.id)
    src = cfg["sources"]["openmeteo"]
    lats, lons = _grid(region.compute, src["resolution"])
    pts = [(la, lo) for la in lats for lo in lons]
    times = [run.init + timedelta(hours=s) for s in steps]
    t_index = {t: k for k, t in enumerate(times)}
    out = {v: np.full((len(times), len(lats), len(lons)), np.nan, dtype=np.float32) for v in VARS.values()}
    chunk = src["points_per_request"]
    for c0 in range(0, len(pts), chunk):
        progress(f"Open-Meteo {run.model} {c0 // chunk + 1}/{-(-len(pts) // chunk)}", c0 / len(pts))
        part = pts[c0:c0 + chunk]
        params = {
            "latitude": ",".join(f"{p[0]:.2f}" for p in part),
            "longitude": ",".join(f"{p[1]:.2f}" for p in part),
            "hourly": ",".join(VARS),
            "models": MODELS[run.model][0],
            "timezone": "GMT",
            "cell_selection": "nearest",
            "start_hour": times[0].strftime("%Y-%m-%dT%H:%M"),
            "end_hour": times[-1].strftime("%Y-%m-%dT%H:%M"),
        }
        resp = http.get(src["forecast_url"], retries=src["retries"], timeout=src["timeout_s"], params=params)
        resp.raise_for_status()
        body = resp.json()
        for p, loc in zip(range(c0, c0 + len(part)), body if isinstance(body, list) else [body]):
            j, i = divmod(p, len(lons))
            for t_str, *vals in zip(loc["hourly"]["time"], *(loc["hourly"][v] for v in VARS)):
                k = t_index.get(datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc))
                if k is None:
                    continue
                for name, v in zip(VARS.values(), vals):
                    out[name][k, j, i] = np.nan if v is None else v / 100.0
        time.sleep(src["request_interval_s"])
    for k, s in enumerate(steps):
        if any(np.isnan(out[v][k]).all() for v in VARS.values()):
            raise RuntimeError(f"Open-Meteo returned no data for {times[k]:%Y-%m-%d %H}Z")
        save_step(cache_path(cfg, run, s, region.id), lats, lons,
                  *(np.nan_to_num(out[v][k], nan=0.0) for v in ("low", "mid", "high")))
    log.info("Open-Meteo %s: %d points × %d times", run.model, len(pts), len(times))
    return load_cached(cfg, run, steps, region.id)
