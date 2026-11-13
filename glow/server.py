"""Local web server: serves site/, manages user-drawn regions and refreshes individual days in a background worker."""
import json
import logging
import shutil
import threading
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from glow import archive, regions, spots
from glow.config import ROOT
from glow.detail import Context, point_detail
from glow.pipeline import cleanup_cache, region_dir, run_region, run_region_day

log = logging.getLogger(__name__)

SITE_DIR = ROOT / "site"
MAX_BODY = 4096


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RefreshJob:
    region: str
    date: str | None = None


class Worker:
    """Runs region or single-day jobs one at a time from a de-duplicated queue."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cond = threading.Condition()
        self.queue = []
        self.current = None
        self.stage = ""
        self.fraction = None
        self.started_at = None
        self.errors = {}
        self.finished_at = None
        threading.Thread(target=self._loop, daemon=True).start()

    def enqueue(self, jobs):
        with self.cond:
            for job in jobs:
                if job not in self.queue and job != self.current:
                    self.queue.append(job)
            self.cond.notify()

    def _progress(self, msg, frac=None):
        with self.cond:
            self.stage = msg
            # A source falling back to another restarts its own count; never let the bar move backwards
            if frac is not None:
                self.fraction = max(self.fraction or 0.0, min(frac, 1.0))

    def _loop(self):
        while True:
            with self.cond:
                while not self.queue:
                    self.cond.wait()
                job = self.queue.pop(0)
                self.current, self.stage = job, "准备"
                self.fraction, self.started_at = 0.0, _now()
            region = regions.get(self.cfg, job.region)
            error = None
            if region is not None:
                try:
                    if job.date:
                        run_region_day(self.cfg, region, job.date, progress=self._progress)
                    else:
                        run_region(self.cfg, region, progress=self._progress)
                except Exception as e:
                    log.error("region %s %s failed\n%s", job.region, job.date, traceback.format_exc())
                    error = {"error": str(e), "date": job.date}
                # Deleted while it was being computed: drop what the run just wrote
                if regions.get(self.cfg, job.region) is None:
                    shutil.rmtree(region_dir(self.cfg, job.region), ignore_errors=True)
            with self.cond:
                if error:
                    self.errors[job.region] = {**error, "at": _now()}
                else:
                    self.errors.pop(job.region, None)
                self.current, self.stage, self.finished_at = None, "", _now()
                self.fraction = self.started_at = None
                if not self.queue:
                    cleanup_cache(self.cfg, {r.id for r in regions.load_all(self.cfg)}, datetime.now(timezone.utc))

    def snapshot(self):
        with self.cond:
            return {"state": "running" if self.current or self.queue else "idle",
                    "current": self.current.region if self.current else None,
                    "current_date": self.current.date if self.current else None,
                    "stage": self.stage,
                    "progress": None if self.fraction is None else round(self.fraction, 3),
                    "started_at": self.started_at,
                    "queue": [{"region": job.region, "date": job.date} for job in self.queue],
                    "errors": dict(self.errors), "finished_at": self.finished_at}


class Details:
    """Point details against each region's current manifest; contexts are rebuilt when a manifest changes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ctx = {}

    def get(self, region_id, lat, lon, event, day, snapshot=None):
        if snapshot:
            key = (region_id, snapshot, event, day)
            with self.lock:
                ctx = self.ctx.get(key)
                if ctx is None:
                    ctx = self.ctx[key] = archive.load_point_context(
                        self.cfg, region_id, snapshot, event, day
                    )
            return point_detail(ctx, lat, lon, event, day)
        region = regions.get(self.cfg, region_id)
        if region is None:
            raise ValueError("区域不存在")
        path = region_dir(self.cfg, region_id) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(region_id)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entry = next((e for e in manifest["events"].get(event, []) if e["date"] == day), None)
        if entry is None:
            raise ValueError(f"没有 {day} 的{'晚霞' if event == 'sunset' else '朝霞'}预报")
        runs = entry.get("runs", manifest["runs"])
        key = (region_id, event, day)
        with self.lock:
            ctx = self.ctx.get(key)
            if (ctx is None or ctx.manifest["generated_at"] != manifest["generated_at"]
                    or ctx.manifest["runs"] != runs):
                ctx = self.ctx[key] = Context(self.cfg, region, {**manifest, "runs": runs})
        return point_detail(ctx, lat, lon, event, day)


class Handler(SimpleHTTPRequestHandler):
    cfg = None
    worker = None
    details = None

    def end_headers(self):
        # Data files are rewritten in place by every refresh, so never let the browser reuse them.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def translate_path(self, path):
        # Results live in the configured output directory rather than under site/, so /data/ is mapped there
        if urlsplit(path).path.startswith("/data/"):
            site_dir = self.directory
            self.directory = str(self.cfg["paths"]["output"])
            try:
                return super().translate_path(path[len("/data"):])
            finally:
                self.directory = site_dir
        return super().translate_path(path)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/status":
            return self._json(self.worker.snapshot())
        if url.path == "/api/regions":
            rc = self.cfg["regions"]
            limits = {"frame": rc["frame"], "max_abs_lat": rc["max_abs_lat"],
                      "forecast_days": self.cfg["forecast"]["days"]}
            return self._json({"regions": self._region_list(), "limits": limits,
                               "spots": spots.load_all(self.cfg), "map": self.cfg["map"]})
        if url.path == "/api/archive":
            return self._json({"regions": archive.list_archives(self.cfg)})
        if url.path == "/api/point":
            return self._point(parse_qs(url.query))
        return super().do_GET()

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        path = urlsplit(self.path).path
        if path == "/api/refresh":
            return self._refresh()
        if path == "/api/regions":
            return self._create_region()
        if path == "/api/spots":
            return self._create_spot()
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        path = urlsplit(self.path).path
        if path.startswith("/api/archive/"):
            parts = path[len("/api/archive/"):].split("/")
            if len(parts) == 1 and archive.delete_region(self.cfg, parts[0]):
                with self.details.lock:
                    for key in [k for k in self.details.ctx if k[0] == parts[0] and len(k) == 4]:
                        self.details.ctx.pop(key)
                return self._json({"deleted": parts[0], "scope": "archive"})
            if len(parts) == 2 and archive.delete_snapshot(self.cfg, parts[0], parts[1]):
                with self.details.lock:
                    for key in [k for k in self.details.ctx
                                if k[0] == parts[0] and len(k) == 4 and k[1] == parts[1]]:
                        self.details.ctx.pop(key)
                return self._json({"deleted": parts[1], "region": parts[0], "scope": "snapshot"})
            return self._json({"error": "存档不存在"}, 404)
        if path.startswith("/api/spots/"):
            sid = path.rsplit("/", 1)[1]
            if not spots.delete(self.cfg, sid):
                return self._json({"error": "机位不存在"}, 404)
            return self._json({"deleted": sid})
        if not path.startswith("/api/regions/"):
            return self._json({"error": "not found"}, 404)
        rid = path.rsplit("/", 1)[1]
        if not regions.delete(self.cfg, rid):
            return self._json({"error": "区域不存在"}, 404)
        shutil.rmtree(region_dir(self.cfg, rid), ignore_errors=True)
        with self.details.lock:
            for key in [k for k in self.details.ctx if k[0] == rid]:
                self.details.ctx.pop(key)
        return self._json({"deleted": rid})

    def _same_origin(self):
        # Only this page may change state; other sites open in the browser cannot post here cross-origin.
        # The Host check also stops DNS rebinding, where a foreign name resolves to this machine and
        # Origin and Host then agree with each other.
        host = self.headers.get("Host", "")
        if urlsplit("//" + host).hostname not in self.cfg["server"]["allowed_hosts"]:
            return False
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == host

    def _region_list(self):
        st = self.worker.snapshot()
        pending_dates = {}
        if st["current"]:
            pending_dates[st["current"]] = [st["current_date"]] if st["current_date"] else []
        for job in st["queue"]:
            dates = pending_dates.setdefault(job["region"], [])
            if job["date"]:
                dates.append(job["date"])
        out = []
        for r in regions.load_all(self.cfg):
            path = region_dir(self.cfg, r.id) / "manifest.json"
            m = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            out.append({**r.to_json(),
                        "computed_at": m["generated_at"] if m else None,
                        "pending": bool(pending_dates.get(r.id)),
                        "pending_dates": pending_dates.get(r.id, []),
                        "error": st["errors"].get(r.id)})
        return out

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            raise ValueError("请求太大")
        return json.loads(self.rfile.read(length) or b"{}")

    def _refresh(self):
        try:
            body = self._read_json()
            rid = str(body["region"])
            day = body["date"]
            if not isinstance(day, str):
                raise ValueError
            target = date.fromisoformat(day)
            if target.isoformat() != day:
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._json({"error": "需要 region 和 date（YYYY-MM-DD）"}, 400)
        region = regions.get(self.cfg, rid)
        if region is None:
            return self._json({"error": "区域不存在"}, 404)
        path = region_dir(self.cfg, rid) / "manifest.json"
        if not path.exists():
            return self._json({"error": "这个区域还没有计算结果"}, 400)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._json({"error": "区域结果读取失败"}, 500)
        dates = {entry.get("date") for entries in manifest.get("events", {}).values() for entry in entries}
        if day not in dates:
            today = datetime.now(timezone.utc).astimezone(ZoneInfo(region.timezone)).date()
            latest = today + timedelta(days=self.cfg["forecast"]["days"])
            if target < today:
                return self._json({"error": f"{day} 没有历史记录"}, 400)
            if target > latest:
                return self._json({"error": f"未来只能补算到 {latest.isoformat()}"}, 400)
        job = RefreshJob(rid, day)
        self.worker.enqueue([job])
        return self._json({"queued": [{"region": rid, "date": day}], **self.worker.snapshot()})

    def _create_region(self):
        try:
            body = self._read_json()
            region = regions.create(self.cfg, body.get("name"), float(body["lat"]), float(body["lon"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            return self._json({"error": "需要 name、lat、lon（框的中心）"}, 400)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        self.worker.enqueue([RefreshJob(region.id)])
        return self._json(region.to_json(), 201)

    def _create_spot(self):
        try:
            body = self._read_json()
            spot = spots.create(self.cfg, body.get("name"), float(body["lat"]), float(body["lon"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            return self._json({"error": "需要 name、lat、lon"}, 400)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        return self._json(spot, 201)

    def _point(self, q):
        try:
            rid = q["region"][0]
            lat, lon = float(q["lat"][0]), float(q["lon"][0])
            event, day = q["event"][0], q["date"][0]
            snapshot = q.get("snapshot", [None])[0]
        except (KeyError, ValueError):
            return self._json({"error": "需要 region、lat、lon、event、date 参数"}, 400)
        try:
            return self._json(self.details.get(rid, lat, lon, event, day, snapshot=snapshot))
        except FileNotFoundError:
            message = "存档不存在或已被删除" if snapshot else "这个区域还没有算完"
            return self._json({"error": message}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


def make_server(cfg, host, port):
    Handler.cfg = cfg
    Handler.worker = Worker(cfg)
    Handler.details = Details(cfg)
    return ThreadingHTTPServer((host, port), partial(Handler, directory=str(SITE_DIR)))


def serve(cfg):
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    httpd = make_server(cfg, host, port)
    log.info("serving on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
