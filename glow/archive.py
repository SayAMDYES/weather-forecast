"""Permanent, user-managed snapshots of successful region computations."""
import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from glow.config import Box, compute_box_for
from glow.regions import Region
from glow.sources.base import CloudField
from glow.terrain import Terrain

_lock = threading.RLock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def root(cfg):
    return cfg["paths"]["archive"]


def region_dir(cfg, region_id):
    return root(cfg) / region_id


def _index_path(cfg, region_id):
    return region_dir(cfg, region_id) / "index.json"


def _read_index(cfg, region_id):
    path = _index_path(cfg, region_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_index(cfg, region_id, index):
    path = _index_path(cfg, region_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _sort_key(item):
    return (item.get("generated_at") or "", item.get("archived_at") or "")


def _run_meta(run):
    return {key: run.get(key) for key in ("init", "source", "resolution", "tag") if run.get(key) is not None}


def _snapshot_runs(manifest, events):
    runs = {}
    for entries in events.values():
        for entry in entries:
            entry_runs = entry.get("runs") or manifest.get("runs", {})
            for model, run in entry_runs.items():
                init = run.get("init")
                if init:
                    runs.setdefault(model, {}).setdefault(init, _run_meta(run))
    return {model: [items[init] for init in sorted(items)] for model, items in runs.items()}


def _public_path(cfg, path):
    try:
        return path.relative_to(cfg["paths"]["output"]).as_posix()
    except ValueError as e:
        raise ValueError("存档目录必须位于输出目录内，才能在网页中读取") from e


def _safe_id(value):
    return bool(value and _SAFE_ID.fullmatch(value))


def has_snapshots(cfg, region_id):
    index = _read_index(cfg, region_id)
    return bool(index and index.get("snapshots"))


def _snapshot_id(cfg, region_id, now, kind):
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"legacy-{stamp}" if kind == "legacy" else stamp
    candidate = base
    suffix = 2
    while (region_dir(cfg, region_id) / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _save_fields(path, fields):
    if not fields:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez_compressed(
        tmp,
        times=np.array([field.valid_time.isoformat() for field in fields]),
        lats=fields[0].lats,
        lons=fields[0].lons,
        low=np.stack([field.low for field in fields]),
        mid=np.stack([field.mid for field in fields]),
        high=np.stack([field.high for field in fields]),
    )
    tmp.replace(path)
    return True


def _load_fields(path):
    with np.load(path) as data:
        times = data["times"].tolist()
        lats = data["lats"].copy()
        lons = data["lons"].copy()
        low = data["low"].copy()
        mid = data["mid"].copy()
        high = data["high"].copy()
    return [
        CloudField(datetime.fromisoformat(times[i]), lats, lons, low[i], mid[i], high[i])
        for i in range(len(times))
    ]


def _save_terrain(path, terrain):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez_compressed(
        tmp,
        lats=terrain.lats,
        lons=terrain.lons,
        max_m=np.rint(terrain.max_m).astype(np.int16),
        mean_m=np.rint(terrain.mean_m).astype(np.int16),
    )
    tmp.replace(path)


def _load_terrain(path):
    with np.load(path) as data:
        return Terrain(data["lats"].copy(), data["lons"].copy(), data["max_m"].copy(), data["mean_m"].copy())


def _copy_file(cfg, source_rel, dest):
    source = cfg["paths"]["output"] / source_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return _public_path(cfg, dest)


def write_snapshot(cfg, region, manifest, events, terrain, archived_at=None, kind="run"):
    """Copy one successful calculation into an immutable archive snapshot.

    events entries must contain event, date, entry and fields (model → list[CloudField]).
    """
    archived_at = archived_at or datetime.now(timezone.utc)
    if not events:
        return None

    with _lock:
        snapshot = _snapshot_id(cfg, region.id, archived_at, kind)
        snap_dir = region_dir(cfg, region.id) / snapshot
        snap_dir.mkdir(parents=True, exist_ok=False)
        event_keys = list(manifest.get("events", {}))
        archived_events = {event: [] for event in event_keys}
        all_details = bool(manifest.get("models"))

        try:
            terrain_path = region_dir(cfg, region.id) / "terrain.npz"
            if not terrain_path.exists():
                _save_terrain(terrain_path, terrain)
            terrain_rel = _public_path(cfg, terrain_path)

            for item in events:
                event, day, entry = item["event"], item["date"], item["entry"]
                model_files = {}
                for model, source_rel in entry.get("files", {}).items():
                    dest = snap_dir / "grid" / Path(source_rel).name
                    model_files[model] = _copy_file(cfg, source_rel, dest)

                sun_rel = _copy_file(cfg, entry["sun"], snap_dir / "sun" / Path(entry["sun"]).name)
                detail_fields = {}
                for model, fields in item.get("fields", {}).items():
                    if not fields:
                        continue
                    dest = snap_dir / "fields" / f"{day}_{event}_{model}.npz"
                    if _save_fields(dest, fields):
                        detail_fields[model] = _public_path(cfg, dest)
                models = set(manifest.get("models", []))
                if set(detail_fields) != models or set(model_files) != models:
                    all_details = False

                archived_events.setdefault(event, []).append({
                    "date": day,
                    "files": model_files,
                    "sun": sun_rel,
                    "runs": entry.get("runs", manifest.get("runs", {})),
                    "detail_fields": detail_fields,
                })

            for entries in archived_events.values():
                entries.sort(key=lambda entry: entry["date"])

            snapshot_manifest = {
                **{key: value for key, value in manifest.items() if key != "events"},
                "scoring": cfg["scoring"],
                "archive": {
                    "snapshot": snapshot,
                    "kind": kind,
                    "archived_at": archived_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "point_details": all_details,
                },
                "terrain": terrain_rel,
                "events": archived_events,
            }
            manifest_path = snap_dir / "manifest.json"
            manifest_tmp = manifest_path.with_suffix(".tmp")
            manifest_tmp.write_text(
                json.dumps(snapshot_manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            manifest_tmp.replace(manifest_path)

            dates = sorted({item["date"] for item in events})
            present_events = sorted({item["event"] for item in events})
            summary = {
                "snapshot": snapshot,
                "kind": kind,
                "archived_at": archived_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "generated_at": manifest.get("generated_at"),
                "date_min": dates[0],
                "date_max": dates[-1],
                "events": present_events,
                "models": manifest.get("models", []),
                "runs": _snapshot_runs(manifest, archived_events),
                "point_details": all_details,
                "manifest": _public_path(cfg, manifest_path),
            }
            index = _read_index(cfg, region.id) or {
                "region": region.to_json(),
                "snapshots": [],
            }
            index["region"] = region.to_json()
            index["updated_at"] = archived_at.astimezone(timezone.utc).isoformat(timespec="seconds")
            index["snapshots"] = [s for s in index.get("snapshots", []) if s.get("snapshot") != snapshot]
            index["snapshots"].append(summary)
            index["snapshots"].sort(key=_sort_key, reverse=True)
            _write_index(cfg, region.id, index)
            return summary
        except Exception:
            shutil.rmtree(snap_dir, ignore_errors=True)
            raise


def list_archives(cfg):
    out = []
    archive_root = root(cfg)
    if not archive_root.exists():
        return out
    with _lock:
        for path in sorted(archive_root.iterdir()):
            if not path.is_dir() or not _safe_id(path.name):
                continue
            index = _read_index(cfg, path.name)
            if not index or not index.get("region"):
                continue
            snapshots = []
            for item in index.get("snapshots", []):
                manifest = cfg["paths"]["output"] / item.get("manifest", "")
                if item.get("manifest") and manifest.exists():
                    if not item.get("runs"):
                        try:
                            snapshot_manifest = json.loads(manifest.read_text(encoding="utf-8"))
                            item = {**item, "runs": _snapshot_runs(snapshot_manifest,
                                                                   snapshot_manifest.get("events", {}))}
                        except (OSError, json.JSONDecodeError):
                            pass
                    snapshots.append(item)
            if not snapshots:
                continue
            snapshots.sort(key=_sort_key, reverse=True)
            out.append({**index["region"], "snapshots": snapshots})
    return out


def load_snapshot_manifest(cfg, region_id, snapshot):
    if not _safe_id(region_id) or not _safe_id(snapshot):
        raise ValueError("存档标识无效")
    with _lock:
        index = _read_index(cfg, region_id)
        item = next((entry for entry in (index or {}).get("snapshots", [])
                     if entry.get("snapshot") == snapshot), None)
        if item is None:
            raise FileNotFoundError(snapshot)
        path = cfg["paths"]["output"] / item["manifest"]
    if not path.exists():
        raise FileNotFoundError(snapshot)
    return json.loads(path.read_text(encoding="utf-8"))


def _region_from_manifest(cfg, manifest):
    data = manifest["region"]
    display = Box(**data["display"])
    return Region(
        data["id"],
        data["name"],
        display,
        compute_box_for(display, cfg["scoring"]["ray"]["max_km"]),
        data["timezone"],
        data["created_at"],
    )


def load_point_context(cfg, region_id, snapshot, event, day):
    manifest = load_snapshot_manifest(cfg, region_id, snapshot)
    entry = next((entry for entry in manifest.get("events", {}).get(event, []) if entry.get("date") == day), None)
    if entry is None:
        raise ValueError(f"存档里没有 {day} 的{'晚霞' if event == 'sunset' else '朝霞'}数据")
    detail_fields = entry.get("detail_fields", {})
    if not detail_fields:
        raise ValueError("这份存档只包含地图，没有点位重算数据")
    terrain_rel = manifest.get("terrain")
    if not terrain_rel:
        raise ValueError("这份存档缺少地形数据")
    output_root = cfg["paths"]["output"]
    terrain_path = output_root / terrain_rel
    if not terrain_path.exists():
        raise ValueError("这份存档的地形数据已丢失")
    fields = {}
    for model, rel in detail_fields.items():
        path = output_root / rel
        if path.exists():
            fields[model] = _load_fields(path)
    if not fields:
        raise ValueError("这份存档的点位云场数据已丢失")
    archived_cfg = {**cfg, "scoring": manifest.get("scoring", cfg["scoring"])}
    return SimpleNamespace(
        cfg=archived_cfg,
        region=_region_from_manifest(cfg, manifest),
        manifest=manifest,
        fields=fields,
        terrain=_load_terrain(terrain_path),
    )


def delete_snapshot(cfg, region_id, snapshot):
    if not _safe_id(region_id) or not _safe_id(snapshot):
        return False
    with _lock:
        index = _read_index(cfg, region_id)
        if not index:
            return False
        kept = [item for item in index.get("snapshots", []) if item.get("snapshot") != snapshot]
        if len(kept) == len(index.get("snapshots", [])):
            return False
        shutil.rmtree(region_dir(cfg, region_id) / snapshot, ignore_errors=True)
        if not kept:
            shutil.rmtree(region_dir(cfg, region_id), ignore_errors=True)
        else:
            index["snapshots"] = kept
            index["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _write_index(cfg, region_id, index)
        return True


def delete_region(cfg, region_id):
    if not _safe_id(region_id):
        return False
    with _lock:
        path = region_dir(cfg, region_id)
        if not path.exists():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True
