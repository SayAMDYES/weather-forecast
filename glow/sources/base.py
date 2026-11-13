from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np


@dataclass
class CloudField:
    """Low/mid/high cloud cover (fractions 0–1) at one valid time on a regular grid with ascending lats/lons.

    member indexes ensemble members; deterministic runs use member 0.
    """
    valid_time: datetime
    lats: np.ndarray
    lons: np.ndarray
    low: np.ndarray
    mid: np.ndarray
    high: np.ndarray
    member: int = 0


@dataclass
class RunInfo:
    model: str
    init: datetime
    source: str
    resolution: float
    # Cache sub-directory; runs fetched from different products of one model must not share files
    tag: str


def steps_for(init, windows, step_hours, max_step):
    """Forecast steps (hours) bracketing every [start, end] window of valid times, or None if out of range."""
    steps = set()
    for start, end in windows:
        h0 = (start - init).total_seconds() / 3600
        h1 = (end - init).total_seconds() / 3600
        first = int(np.floor(h0 / step_hours)) * step_hours
        last = int(np.ceil(h1 / step_hours)) * step_hours
        if first < 0 or last > max_step:
            return None
        steps.update(range(first, last + 1, step_hours))
    return sorted(steps)


def run_dir(cfg, run):
    return cfg["paths"]["cache"] / run.model / run.tag / run.init.strftime("%Y%m%d%H")


def cache_path(cfg, run, step, region_id):
    """Fields cropped to one region's compute box."""
    return run_dir(cfg, run) / region_id / f"f{step:03d}.npz"


def save_step(path, lats, lons, low, mid, high):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez_compressed(tmp, lats=lats, lons=lons, low=low.astype(np.float32),
                        mid=mid.astype(np.float32), high=high.astype(np.float32))
    tmp.replace(path)


def load_cached(cfg, run, steps, region_id):
    fields = []
    for step in steps:
        with np.load(cache_path(cfg, run, step, region_id)) as z:
            fields.append(CloudField(run.init + timedelta(hours=step), z["lats"], z["lons"],
                                     z["low"], z["mid"], z["high"]))
    return fields
