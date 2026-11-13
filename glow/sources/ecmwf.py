"""ECMWF IFS open data (CC-BY-4.0): lcc/mcc/hcc fetched by byte range from the data.ecmwf.int index files.

0.1° is preferred; it is published about two hours after 0.25°, so a run only out at 0.25° is used as is.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

from glow.sources import http
from glow.sources.base import RunInfo, cache_path, load_cached, run_dir, save_step, steps_for
from glow.sources.grib import crop, decode

log = logging.getLogger(__name__)

MODEL = "ecmwf"
PARAMS = {"lcc": "low", "mcc": "mid", "hcc": "high"}
# 3-hourly steps run to 144 h in every cycle (00/12z go further at 6-hourly)
MAX_STEP = 144
RESOLUTIONS = {"0p1": 0.1, "0p25": 0.25}


def _url(base, init, resol, step):
    # 0.1° files are split into single-level (-sfc) and pressure-level (-pl) files
    suffix = "-sfc" if resol == "0p1" else ""
    return f"{base}/{init:%Y%m%d}/{init:%H}z/ifs/{resol}/oper/{init:%Y%m%d%H}0000-{step}h-oper-fc{suffix}"


def find_run(cfg, windows, now=None):
    src = cfg["sources"]["ecmwf"]
    now = now or datetime.now(timezone.utc)
    latest = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
    for k in range(src["lookback_cycles"]):
        init = latest - timedelta(hours=6 * k)
        steps = steps_for(init, windows, 3, MAX_STEP)
        if steps is None:
            continue
        for resol in src["resolutions"]:
            if http.exists(_url(src["base_url"], init, resol, steps[-1]) + ".index"):
                return RunInfo(MODEL, init, "ecmwf", RESOLUTIONS[resol], resol), steps
        log.info("ECMWF %s not published yet", init.strftime("%Y-%m-%d %HZ"))
    raise RuntimeError("no published ECMWF run found")


def load_steps(cfg, run, steps, region, progress=lambda msg, frac=None: None):
    """CloudFields over the region's compute box. The global GRIB of each step is downloaded once per run
    and kept, since the portal cannot cut out an area; every region is then cropped from it locally."""
    todo = [s for s in steps if not _raw_path(cfg, run, s).exists()]
    if todo:
        done = [0]

        def fetch(step):
            _fetch_raw(cfg, run, step)
            done[0] += 1
            progress(f"EC 云量 {done[0]}/{len(todo)}", done[0] / len(todo))

        # A few parallel connections; the portal allows far more but this is plenty for ~15 files
        with ThreadPoolExecutor(max_workers=cfg["sources"]["ecmwf"]["parallel"]) as ex:
            list(ex.map(fetch, todo))
    for step in steps:
        path = cache_path(cfg, run, step, region.id)
        if not path.exists():
            save_step(path, **_crop(_raw_path(cfg, run, step).read_bytes(), region.compute, run.resolution))
    return load_cached(cfg, run, steps, region.id)


def _raw_path(cfg, run, step):
    return run_dir(cfg, run) / "raw" / f"f{step:03d}.grib2"


def _fetch_raw(cfg, run, step):
    src = cfg["sources"]["ecmwf"]
    url = _url(src["base_url"], run.init, run.tag, step)
    idx = http.get(url + ".index", retries=src["retries"], timeout=src["timeout_s"])
    idx.raise_for_status()
    entries = [json.loads(line) for line in idx.text.splitlines() if line.strip()]
    wanted = sorted((e["_offset"], e["_length"]) for e in entries if e.get("param") in PARAMS)
    if len(wanted) != len(PARAMS):
        raise RuntimeError(f"index for step {step} lacks {sorted(PARAMS)}")
    data = b""
    for start, end in _merge_ranges(wanted):
        resp = http.get(url + ".grib2", retries=src["retries"], timeout=src["timeout_s"],
                        headers={"Range": f"bytes={start}-{end}"})
        resp.raise_for_status()
        data += resp.content
    path = _raw_path(cfg, run, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    log.info("ECMWF %s f%03d downloaded (%.1f MB)", run.tag, step, len(data) / 1e6)


def _crop(data, box, resolution):
    out = {}
    for f in decode(data):
        layer = PARAMS.get(f["shortName"])
        if layer:
            lats, lons, values = crop(f["lats"], f["lons"], f["values"], box, margin=resolution)
            out[layer] = np.clip(values, 0, 1)
            out["lats"], out["lons"] = lats, lons
    return out


def _merge_ranges(ranges):
    """Turn (offset, length) pairs into inclusive byte ranges, joining adjacent ones."""
    merged = []
    for off, length in ranges:
        if merged and merged[-1][1] + 1 == off:
            merged[-1][1] = off + length - 1
        else:
            merged.append([off, off + length - 1])
    return merged
