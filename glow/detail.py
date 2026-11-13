"""Full breakdown for a single point, computed on demand with the same code that produced the map."""
from datetime import date

import numpy as np

from glow import terrain
from glow.pipeline import bracket, load_fields, score_event
from glow.score import CAUSE_NAMES, REASON_NAMES, explain, sample_distances
from glow.sun import event_detail


class Context:
    """Fields and terrain behind one region manifest; rebuilt when a refresh produces a new manifest."""

    def __init__(self, cfg, region, manifest):
        self.cfg = cfg
        self.region = region
        self.manifest = manifest
        self.fields = load_fields(cfg, region, manifest)
        self.terrain = terrain.load_box(cfg, region.compute)


def _f(x, nd=3):
    return round(float(x), nd)


def point_detail(ctx, lat, lon, event, day):
    cfg = ctx.cfg
    params = cfg["scoring"]
    # Outside the display box the light path may leave the downloaded area, so refuse rather than clamp
    if not ctx.region.display.contains(lat, lon):
        raise ValueError(f"该点不在区域「{ctx.region.name}」内")
    t_event, t_target, azimuth = event_detail(lat, lon, date.fromisoformat(day), event,
                                              params["event_offset_min"][event], ctx.region.timezone)
    lats, lons = np.array([lat]), np.array([lon])
    target = np.array([t_target.timestamp()])
    s = sample_distances(params)
    alphas = params["alphas_deg"]
    models = {}
    for model, fs in ctx.fields.items():
        res = score_event(bracket(fs, t_target, t_target), lats, lons, target, np.array([azimuth]), params, ctx.terrain)
        why = explain(res, res["local"], params)
        path = res["path"]
        models[model] = {
            "score": _f(res["score"][0], 1),
            "low": _f(res["local"].low[0]), "mid": _f(res["local"].mid[0]), "high": _f(res["local"].high[0]),
            "canopy_mid": _f(res["canopy_mid"][0]), "canopy_high": _f(res["canopy_high"][0]),
            "trans_mid": _f(res["trans_mid"][0]), "trans_high": _f(res["trans_high"][0]),
            "vis": _f(res["vis"][0]),
            "reason": {"code": REASON_NAMES[int(why["code"][0])], "cause": CAUSE_NAMES[int(why["cause"][0])],
                       "km": _f(why["km"][0], 0), "layer": CAUSE_NAMES[int(why["layer"][0])]},
            "rays": {
                layer: [{"alpha_deg": a, "T": _f(r["T"][0, k]), "block_km": _f(r["block_km"][0, k], 0),
                         "cause": CAUSE_NAMES[int(r["block_cause"][0, k])], "clear": bool(r["clear"][k])}
                        for k, a in enumerate(alphas)]
                for layer, r in (("mid", res["rays_mid"]), ("high", res["rays_high"]))
            },
            "profile": {
                "low": np.round(path.low[0], 3).tolist(), "mid": np.round(path.mid[0], 3).tolist(),
                "high": np.round(path.high[0], 3).tolist(),
                "terrain_m": np.round(path.terrain_km[0] * 1000).astype(int).tolist(),
            },
        }
        elev = float(res["local"].elev_m[0])
    scores = [m["score"] for m in models.values()]
    return {
        "lat": lat, "lon": lon, "event": event, "date": day, "elev_m": round(elev),
        "event_time": t_event.isoformat(timespec="seconds"),
        "target_time": t_target.isoformat(timespec="seconds"),
        "azimuth": _f(azimuth, 1),
        "avg": _f(np.mean(scores), 1),
        "spread": _f(max(scores) - min(scores), 1) if len(scores) > 1 else None,
        "models": models,
        "geometry": {
            "s_km": s.tolist(), "alphas_deg": alphas, "r_eff_km": params["ray"]["r_eff_km"],
            "canopy_km": {"mid": params["canopy"]["mid_height_km"], "high": params["canopy"]["high_height_km"]},
            "layer_tops_km": {"low": params["layers"]["low_top_km"], "mid": params["layers"]["mid_top_km"]},
        },
    }
