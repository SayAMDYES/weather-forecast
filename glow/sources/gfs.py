"""GFS 0.25° low/mid/high cloud cover: NOMADS grib filter (sub-region), falling back to AWS byte ranges."""
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from glow.sources import http
from glow.sources.base import RunInfo, cache_path, load_cached, save_step, steps_for
from glow.sources.grib import crop, decode

log = logging.getLogger(__name__)

MODEL = "gfs"
RESOLUTION = 0.25
# The 0.25° output is hourly up to 120 h; that is enough for three days ahead.
MAX_HOURLY_STEP = 120
LAYER_BY_SURFACE = {"214": "low", "224": "mid", "234": "high"}
IDX_NAMES = {"low": "LCDC:low cloud layer", "mid": "MCDC:middle cloud layer", "high": "HCDC:high cloud layer"}


def _dir(init):
    return f"gfs.{init:%Y%m%d}/{init:%H}/atmos"


def _file(init, step):
    return f"gfs.t{init:%H}z.pgrb2.0p25.f{step:03d}"


def find_run(cfg, windows, now=None):
    """Latest run whose steps covering all windows are published (checked on AWS, which mirrors NOMADS)."""
    src = cfg["sources"]["gfs"]
    now = now or datetime.now(timezone.utc)
    latest = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
    for k in range(src["lookback_cycles"]):
        init = latest - timedelta(hours=6 * k)
        steps = steps_for(init, windows, 1, MAX_HOURLY_STEP)
        if steps is None:
            continue
        if http.exists(f"{src['aws_url']}/{_dir(init)}/{_file(init, steps[-1])}.idx"):
            return RunInfo(MODEL, init, "nomads", RESOLUTION, "0p25"), steps
        log.info("GFS %s not complete yet", init.strftime("%Y-%m-%d %HZ"))
    raise RuntimeError("no complete GFS run found")


def load_steps(cfg, run, steps, region, progress=lambda msg, frac=None: None):
    """CloudFields over the region's compute box, cached or downloaded (NOMADS first, AWS as fallback)."""
    src = cfg["sources"]["gfs"]
    for k, step in enumerate(steps):
        path = cache_path(cfg, run, step, region.id)
        if not path.exists():
            progress(f"GFS 云量 {k + 1}/{len(steps)}", k / len(steps))
            save_step(path, **_download(cfg, run, step, region.compute))
            time.sleep(src["request_interval_s"])
    return load_cached(cfg, run, steps, region.id)


def _download(cfg, run, step, box):
    try:
        return _from_nomads(cfg, run.init, step, box)
    except Exception as e:
        log.warning("NOMADS failed for f%03d (%s); trying AWS", step, e)
        run.source = "aws"
        return _from_aws(cfg, run.init, step, box)


def _to_layers(fields, box):
    out = {}
    for f in fields:
        layer = LAYER_BY_SURFACE.get(f["typeOfFirstFixedSurface"])
        if layer is None or f["stepType"] != "instant":
            continue
        lats, lons, values = crop(f["lats"], f["lons"], f["values"], box, margin=RESOLUTION)
        out[layer] = np.clip(values / 100.0, 0, 1).astype(np.float32)
        out["lats"], out["lons"] = lats, lons
    missing = {"low", "mid", "high"} - out.keys()
    if missing:
        raise RuntimeError(f"GRIB missing layers {sorted(missing)}")
    return out


def _from_nomads(cfg, init, step, box):
    src = cfg["sources"]["gfs"]
    params = {
        "dir": "/" + _dir(init), "file": _file(init, step),
        "var_LCDC": "on", "var_MCDC": "on", "var_HCDC": "on",
        "lev_low_cloud_layer": "on", "lev_middle_cloud_layer": "on", "lev_high_cloud_layer": "on",
        "subregion": "",
        "toplat": box.lat_max + RESOLUTION, "bottomlat": box.lat_min - RESOLUTION,
        "leftlon": box.lon_min - RESOLUTION, "rightlon": box.lon_max + RESOLUTION,
    }
    resp = http.get(src["nomads_url"], retries=src["retries"], timeout=src["timeout_s"], params=params)
    if not resp.ok or not resp.content.startswith(b"GRIB"):
        raise RuntimeError(f"NOMADS returned {resp.status_code}: {resp.content[:120]!r}")
    return _to_layers(decode(resp.content), box)


def _from_aws(cfg, init, step, box):
    """Fetch only the three cloud messages from the global file using the .idx byte offsets."""
    src = cfg["sources"]["gfs"]
    url = f"{src['aws_url']}/{_dir(init)}/{_file(init, step)}"
    idx = http.get(url + ".idx", retries=src["retries"], timeout=src["timeout_s"])
    idx.raise_for_status()
    lines = [ln.split(":") for ln in idx.text.strip().splitlines()]
    want = "anl" if step == 0 else f"{step} hour fcst"
    chunks = []
    for n, parts in enumerate(lines):
        name = f"{parts[3]}:{parts[4]}"
        if name in IDX_NAMES.values() and parts[5] == want:
            start = int(parts[1])
            end = int(lines[n + 1][1]) - 1 if n + 1 < len(lines) else ""
            resp = http.get(url, retries=src["retries"], timeout=src["timeout_s"],
                            headers={"Range": f"bytes={start}-{end}"})
            resp.raise_for_status()
            chunks.append(resp.content)
    return _to_layers(decode(b"".join(chunks)), box)
