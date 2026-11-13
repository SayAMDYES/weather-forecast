"""Per region: download → interpolate to each point's event time → trace light paths → write map data."""
import json
import logging
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from glow import archive, output, spots, terrain
from glow.geometry import bilinear, destination
from glow.score import Local, Path, evaluate, sample_distances
from glow.sources import ecmwf, gfs, openmeteo
from glow.sources.base import RunInfo, load_cached
from glow.sun import event_grid

log = logging.getLogger(__name__)

PRIMARY = {"gfs": gfs, "ecmwf": ecmwf}


def _utc(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc)


def _span(progress, start, end):
    """Report a stage's own 0–1 progress as its slice [start, end] of the whole job."""
    return lambda msg, frac=None: progress(msg, start + (end - start) * (frac or 0.0))


def region_dir(cfg, region_id):
    return cfg["paths"]["output"] / "regions" / region_id


def _relative_output(region, rel):
    prefix = f"regions/{region.id}/"
    return rel[len(prefix):] if rel.startswith(prefix) else rel


def _merge_history(cfg, region, events, keep, now):
    """Keep earlier events still on disk, together with the runs that produced them."""
    path = region_dir(cfg, region.id) / "manifest.json"
    if not path.exists():
        return
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    cutoff = now.astimezone(ZoneInfo(region.timezone)).date() - timedelta(days=cfg["forecast"]["keep_days"] - 1)
    old_runs = old.get("runs", {})
    for event, entries in old.get("events", {}).items():
        if event not in events:
            continue
        dates = {e["date"] for e in events[event]}
        for entry in entries:
            if entry.get("date") in dates or entry.get("date", "") < cutoff.isoformat():
                continue
            rels = list(entry.get("files", {}).values()) + [entry.get("sun")]
            if any(not rel or not (region_dir(cfg, region.id) / _relative_output(region, rel)).exists()
                   for rel in rels):
                continue
            events[event].append({**entry, "runs": entry.get("runs", old_runs)})
            keep.update(_relative_output(region, rel) for rel in rels)
    for entries in events.values():
        entries.sort(key=lambda e: e["date"])


def _legacy_archive_events(cfg, region, manifest, ter):
    """Load fields for the pre-archive manifest so its visible days are preserved too."""
    params = cfg["scoring"]
    output_root = cfg["paths"]["output"]
    out = []
    for event, entries in manifest.get("events", {}).items():
        for entry in entries:
            rels = list(entry.get("files", {}).values()) + [entry.get("sun")]
            if any(not rel or not (output_root / rel).exists() for rel in rels):
                continue
            runs = entry.get("runs") or manifest.get("runs", {})
            fields = {}
            for model, run in runs.items():
                try:
                    info = RunInfo(model, datetime.fromisoformat(run["init"]), run["source"],
                                   run["resolution"], run["tag"])
                    fs = load_cached(cfg, info, run["steps"], region.id)
                    g = event_grid(region.display, date.fromisoformat(entry["date"]), event,
                                   params["event_offset_min"][event], region.timezone)
                    t0, t1 = _utc(g["target"].min()), _utc(g["target"].max())
                    fields[model] = bracket(fs, t0, t1)
                except Exception as e:
                    log.warning("legacy archive for %s %s %s has no %s fields: %s",
                                region.id, entry["date"], event, model, e)
            out.append({"event": event, "date": entry["date"], "entry": entry, "fields": fields})
    return out


def _archive_existing_result(cfg, region, ter):
    """One-time migration: preserve the manifest that existed before permanent archive was enabled."""
    if archive.has_snapshots(cfg, region.id):
        return
    path = region_dir(cfg, region.id) / "manifest.json"
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    events = _legacy_archive_events(cfg, region, manifest, ter)
    if events:
        archive.write_snapshot(cfg, region, manifest, events, ter,
                               archived_at=datetime.now(timezone.utc), kind="legacy")


def plan_events(cfg, region, now):
    """The next `days` occurrences of each event whose glow window has not passed yet, in the region's time zone."""
    params = cfg["scoring"]
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    out = []
    for event in cfg["forecast"]["events"]:
        found = 0
        for k in range(cfg["forecast"]["days"] + 1):
            d = today + timedelta(days=k)
            g = event_grid(region.display, d, event, params["event_offset_min"][event], region.timezone)
            if _utc(g["target"].max()) <= now:
                continue
            out.append((event, d, g))
            found += 1
            if found == cfg["forecast"]["days"]:
                break
    return out


def plan_day(cfg, region, day, now=None):
    """Every configured event on one local date whose glow window has not passed yet."""
    params = cfg["scoring"]
    out = []
    for event in cfg["forecast"]["events"]:
        g = event_grid(region.display, day, event,
                       params["event_offset_min"][event], region.timezone)
        if now is None or _utc(g["target"].max()) > now:
            out.append((event, day, g))
    return out


def fetch_model(cfg, model, region, windows, now, progress):
    """Fields from the model's primary source, falling back to Open-Meteo."""
    try:
        info, steps = PRIMARY[model].find_run(cfg, windows, now)
        log.info("%s run %s (%s), steps %s", model, info.init.strftime("%Y-%m-%d %HZ"), info.tag, steps)
        return info, steps, PRIMARY[model].load_steps(cfg, info, steps, region, progress)
    except Exception as e:
        log.warning("%s primary source failed (%s); falling back to Open-Meteo", model, e)
    info, steps = openmeteo.find_run(cfg, model, windows, now)
    return info, steps, openmeteo.load_steps(cfg, info, steps, region, progress)


def bracket(fields, t0, t1):
    """Fields (sorted by time) from the last one at or before t0 to the first one at or after t1."""
    times = [f.valid_time for f in fields]
    i0 = max([k for k, t in enumerate(times) if t <= t0], default=0)
    i1 = min([k for k, t in enumerate(times) if t >= t1], default=len(fields) - 1)
    return fields[i0:i1 + 1]


def _time_weights(target, field_times):
    """Linear-interpolation weights of each (evenly spaced) field time for every point's target time."""
    t = np.array([ft.timestamp() for ft in field_times])
    spacing = t[1] - t[0] if len(t) > 1 else 1.0
    return np.clip(1.0 - np.abs(target[None, :] - t[:, None]) / spacing, 0.0, 1.0)


def _blend(fields, weights, lat_q, lon_q, attr):
    out = np.zeros(lat_q.shape)
    for f, w in zip(fields, weights):
        w = w.reshape(w.shape + (1,) * (lat_q.ndim - 1))
        if np.any(w > 0):
            out += w * bilinear(getattr(f, attr), f.lats, f.lons, lat_q, lon_q)
    return out


def score_event(fields, lats, lons, target, azimuth, params, ter):
    """Score points at their own target times; fields must bracket all target times."""
    s = sample_distances(params)
    weights = _time_weights(target, [f.valid_time for f in fields])
    p_lat, p_lon = destination(lats[:, None], lons[:, None], azimuth[:, None], s[None, :])
    local = Local(*(_blend(fields, weights, lats, lons, a) for a in ("low", "mid", "high")),
                  elev_m=bilinear(ter.mean_m, ter.lats, ter.lons, lats, lons))
    path = Path(*(_blend(fields, weights, p_lat, p_lon, a) for a in ("low", "mid", "high")),
                terrain_km=bilinear(ter.max_m, ter.lats, ter.lons, p_lat, p_lon) / 1000.0)
    res = evaluate(local, path, params)
    res["local"] = local
    res["path"] = path
    return res


def load_fields(cfg, region, manifest):
    """Rebuild the per-model fields a region manifest was computed from (all served from the cache)."""
    out = {}
    for model, r in manifest["runs"].items():
        info = RunInfo(model, datetime.fromisoformat(r["init"]), r["source"], r["resolution"], r["tag"])
        out[model] = load_cached(cfg, info, r["steps"], region.id)
    return out


def _fetch_fields(cfg, region, planned, now, progress):
    windows = [(_utc(g["target"].min()), _utc(g["target"].max())) for _, _, g in planned]
    runs, fields = {}, {}
    models = cfg["forecast"]["models"]
    for i, model in enumerate(models):
        step_progress = _span(progress, i / len(models), (i + 1) / len(models))
        step_progress(f"{model.upper()} 云量")
        try:
            info, steps, fs = fetch_model(cfg, model, region, windows, now, step_progress)
        except Exception as e:
            log.error("%s unavailable from every source: %s", model, e)
            continue
        runs[model] = {"init": info.init.isoformat(), "source": info.source, "resolution": info.resolution,
                       "tag": info.tag, "steps": steps}
        fields[model] = sorted(fs, key=lambda f: f.valid_time)
    if not fields:
        raise RuntimeError("两个模式的数据都获取失败")
    return runs, fields


def _compute_events(cfg, region, planned, fields, ter, out_dir, ny, nx, lats, lons, params,
                    progress=lambda msg, frac=None: None):
    events = {e: [] for e in cfg["forecast"]["events"]}
    archive_events = []
    keep = set()
    total = len(planned) * len(fields)
    for k, (event, d, g) in enumerate(planned):
        target = g["target"].ravel()
        t0, t1 = _utc(target.min()), _utc(target.max())
        entry = {"date": d.isoformat(), "files": {}}
        archive_fields = {}
        for m, (model, fs) in enumerate(fields.items()):
            done = k * len(fields) + m
            progress(f"计算霞光 {done + 1}/{total}", done / total)
            used_fields = bracket(fs, t0, t1)
            archive_fields[model] = used_fields
            res = score_event(used_fields, lats, lons, target, g["azimuth"].ravel(), params, ter)
            name = f"grid/{d.isoformat()}_{event}_{model}.png"
            output.write_grid_png(out_dir / name, {
                "index": res["score"], "low": 100 * res["local"].low,
                "mid": 100 * res["local"].mid, "high": 100 * res["local"].high,
            }, ny, nx)
            entry["files"][model] = f"regions/{region.id}/{name}"
            keep.add(name)
        sun_name = f"sun/{d.isoformat()}_{event}.json"
        c = g["coarse"]
        output.write_json(out_dir / sun_name, {
            "lats": np.round(c["lats"], 4).tolist(), "lons": np.round(c["lons"], 4).tolist(),
            "event": np.round(c["event"]).astype(int).tolist(),
            "azimuth": np.round(c["azimuth"], 2).tolist(),
            "offset_min": params["event_offset_min"][event],
        })
        entry["sun"] = f"regions/{region.id}/{sun_name}"
        keep.add(sun_name)
        events[event].append(entry)
        archive_events.append({"event": event, "date": d.isoformat(), "entry": entry, "fields": archive_fields})
    return events, archive_events, keep


def _protected_runs(cfg):
    """Runs still referenced by a manifest, including historical events."""
    protected = set()
    regions_out = cfg["paths"]["output"] / "regions"
    if not regions_out.exists():
        return protected
    for path in regions_out.glob("*/manifest.json"):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_sets = [m.get("runs", {})]
        run_sets.extend(e.get("runs", {}) for entries in m.get("events", {}).values() for e in entries)
        for runs in run_sets:
            for model, r in runs.items():
                try:
                    init = datetime.fromisoformat(r["init"]).astimezone(timezone.utc).strftime("%Y%m%d%H")
                except (KeyError, TypeError, ValueError):
                    continue
                protected.add((model, r.get("tag"), init))
    return protected


def cleanup_cache(cfg, region_ids, now):
    """Drop cached runs older than a few days and crops of regions that no longer exist."""
    cutoff = now - timedelta(days=cfg["forecast"]["keep_days"])
    protected = _protected_runs(cfg)
    for model in PRIMARY:
        for run in (cfg["paths"]["cache"] / model).glob("*/*"):
            try:
                init = datetime.strptime(run.name, "%Y%m%d%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if init < cutoff and (model, run.parent.name, run.name) not in protected:
                shutil.rmtree(run, ignore_errors=True)
                continue
            for sub in run.iterdir():
                if sub.is_dir() and sub.name != "raw" and sub.name not in region_ids:
                    shutil.rmtree(sub, ignore_errors=True)
    regions_out = cfg["paths"]["output"] / "regions"
    if regions_out.exists():
        for d in regions_out.iterdir():
            if d.name not in region_ids:
                shutil.rmtree(d, ignore_errors=True)


def run_region(cfg, region, now=None, progress=lambda msg, frac=None: None):
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    params = cfg["scoring"]
    box = region.display
    out_dir = region_dir(cfg, region.id)

    lats2, lons2 = np.meshgrid(*box.grid(), indexing="ij")
    ny, nx = lats2.shape
    lats, lons = lats2.ravel(), lons2.ravel()

    progress("地形", 0.0)
    ter = terrain.load_box(cfg, region.compute, _span(progress, 0.0, 0.1))
    try:
        _archive_existing_result(cfg, region, ter)
    except Exception as e:
        log.warning("could not migrate existing results for %s: %s", region.id, e)

    planned = plan_events(cfg, region, now)
    runs, fields = _fetch_fields(cfg, region, planned, now, _span(progress, 0.1, 0.8))

    progress("计算霞光", 0.8)
    events, archive_events, keep = _compute_events(
        cfg, region, planned, fields, ter, out_dir, ny, nx, lats, lons, params,
        progress=_span(progress, 0.8, 0.95),
    )

    _merge_history(cfg, region, events, keep, now)

    manifest = {
        "region": region.to_json(),
        "generated_at": now.isoformat(timespec="seconds"),
        "elapsed_s": round(time.monotonic() - started, 1),
        "timezone": region.timezone,
        "runs": runs,
        "models": list(fields),
        "grid": {"lat_max": box.lat_max, "lat_min": box.lat_min, "lon_min": box.lon_min,
                 "lon_max": box.lon_max, "step": box.step, "nx": nx, "ny": ny,
                 "layers": output.GRID_LAYERS},
        "spots": spots.inside(cfg, box),
        "events": events,
    }
    progress("写入存档", 0.95)
    archive.write_snapshot(cfg, region, manifest, archive_events, ter, archived_at=now, kind="run")
    output.remove_stale(out_dir, keep)
    output.write_json(out_dir / "manifest.json", manifest)
    log.info("region %s (%s) done in %.1f s", region.id, region.name, manifest["elapsed_s"])
    return manifest


def run_region_day(cfg, region, day, now=None, progress=lambda msg, frac=None: None):
    """Recompute one local date and leave every other date's files and run versions untouched."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(day) if isinstance(day, str) else day
    params = cfg["scoring"]
    box = region.display
    out_dir = region_dir(cfg, region.id)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("区域还没有可局部刷新的结果")
    old = json.loads(manifest_path.read_text(encoding="utf-8"))

    lats2, lons2 = np.meshgrid(*box.grid(), indexing="ij")
    ny, nx = lats2.shape
    lats, lons = lats2.ravel(), lons2.ravel()

    progress("地形", 0.0)
    ter = terrain.load_box(cfg, region.compute, _span(progress, 0.0, 0.1))
    try:
        _archive_existing_result(cfg, region, ter)
    except Exception as e:
        log.warning("could not migrate existing results for %s: %s", region.id, e)

    planned = plan_day(cfg, region, day, now)
    if not planned:
        raise ValueError(f"{day.isoformat()} 的霞光时段已过去，无法刷新")
    runs, fields = _fetch_fields(cfg, region, planned, now, _span(progress, 0.1, 0.8))

    progress("计算霞光", 0.8)
    refreshed, archive_events, keep = _compute_events(
        cfg, region, planned, fields, ter, out_dir, ny, nx, lats, lons, params,
        progress=_span(progress, 0.8, 0.95),
    )
    for entries in refreshed.values():
        for entry in entries:
            entry["runs"] = runs

    old_runs = old.get("runs", {})
    event_names = list(dict.fromkeys([*old.get("events", {}), *cfg["forecast"]["events"]]))
    events = {}
    for event in event_names:
        kept = []
        for entry in old.get("events", {}).get(event, []):
            if entry.get("date") == day.isoformat() and event in refreshed:
                continue
            kept.append(entry if entry.get("runs") else {**entry, "runs": old_runs})
        events[event] = kept + refreshed.get(event, [])
        events[event].sort(key=lambda entry: entry["date"])
        for entry in events[event]:
            for rel in list(entry.get("files", {}).values()) + [entry.get("sun")]:
                if rel:
                    keep.add(_relative_output(region, rel))

    manifest = {
        **old,
        "region": region.to_json(),
        "generated_at": now.isoformat(timespec="seconds"),
        "elapsed_s": round(time.monotonic() - started, 1),
        "timezone": region.timezone,
        "runs": runs,
        "models": list(fields),
        "grid": {"lat_max": box.lat_max, "lat_min": box.lat_min, "lon_min": box.lon_min,
                 "lon_max": box.lon_max, "step": box.step, "nx": nx, "ny": ny,
                 "layers": output.GRID_LAYERS},
        "spots": spots.inside(cfg, box),
        "events": events,
    }
    progress("写入存档", 0.95)
    archive.write_snapshot(cfg, region, manifest, archive_events, ter, archived_at=now, kind="run")
    output.remove_stale(out_dir, keep)
    output.write_json(manifest_path, manifest)
    log.info("region %s (%s) %s refreshed in %.1f s", region.id, region.name, day, manifest["elapsed_s"])
    return manifest
