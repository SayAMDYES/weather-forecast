"""Afterglow scoring.

All functions are vectorised over N observer points. Path arrays have shape (N, S) where sample k
lies at distance (k + 1) * step_km toward the sun. Cloud covers are fractions in [0, 1]; heights in km.
"""
from dataclasses import dataclass

import numpy as np

from glow.geometry import ray_height

# Block causes recorded per (point, alpha, canopy layer)
NONE, LOW, MID, HIGH, TERRAIN, HORIZON = 0, 1, 2, 3, 4, 5
CAUSE_NAMES = {NONE: "none", LOW: "low", MID: "mid", HIGH: "high", TERRAIN: "terrain", HORIZON: "horizon"}

# Terrain below this height (km) counts as sea level, so a hit there is reported as the horizon
_SEA_LEVEL_KM = 0.05


@dataclass
class Path:
    low: np.ndarray
    mid: np.ndarray
    high: np.ndarray
    terrain_km: np.ndarray


@dataclass
class Local:
    low: np.ndarray
    mid: np.ndarray
    high: np.ndarray
    elev_m: np.ndarray


def sample_distances(params):
    ray = params["ray"]
    n = int(round(ray["max_km"] / ray["step_km"]))
    return ray["step_km"] * np.arange(1, n + 1, dtype=np.float64)


def _alphas(params):
    # Sunrise mirrors sunset in time, so both use the same set of solar elevations.
    alphas = np.radians(np.asarray(params["alphas_deg"], dtype=np.float64))
    weights = np.asarray(params["alpha_weights"], dtype=np.float64)
    return alphas, weights


def _layer_of(z_km, layers):
    return np.where(z_km < layers["low_top_km"], LOW, np.where(z_km < layers["mid_top_km"], MID, HIGH))


def trace_rays(path, h_km, params):
    """Transmission of each ray lighting a canopy at h_km, plus where and why it was blocked.

    Returns dict of arrays shaped (N, A): T, block_km, block_cause; and clear (A,) — whether the ray
    clears the horizon on a smooth earth, used to normalise against the geometric maximum.
    """
    s = sample_distances(params)
    ds = params["ray"]["step_km"]
    r_eff = params["ray"]["r_eff_km"]
    layers = params["layers"]
    k = params["attenuation_per_km"]
    threshold = params["block_threshold"]
    alphas, _ = _alphas(params)
    own_layer = _layer_of(np.array(h_km), layers)
    # The canopy's own layer right around the observer is the lit screen itself, not an obstacle.
    near = s <= params["self_clearance_km"]

    n = path.low.shape[0]
    a_count = len(alphas)
    T = np.zeros((n, a_count))
    block_km = np.zeros((n, a_count))
    block_cause = np.zeros((n, a_count), dtype=np.int8)
    clear = np.zeros(a_count, dtype=bool)

    for a, alpha in enumerate(alphas):
        z = ray_height(h_km, alpha, s, r_eff)
        clear[a] = bool(np.all(z > 0))
        layer = _layer_of(z, layers)
        cover = np.where(layer == LOW, path.low, np.where(layer == MID, path.mid, path.high))
        coef = np.select([layer == LOW, layer == MID], [k["low"], k["mid"]], k["high"])
        coef = np.where(near & (layer == own_layer), 0.0, coef)
        trans = np.exp(-np.cumsum(coef * cover * ds, axis=1))

        hard = (z <= path.terrain_km) | (z <= 0)
        has_hard = hard.any(axis=1)
        first_hard = np.where(has_hard, hard.argmax(axis=1), len(s))

        weak = trans < threshold
        has_weak = weak.any(axis=1)
        first_weak = np.where(has_weak, weak.argmax(axis=1), len(s))

        rows = np.arange(n)
        T[:, a] = np.where(has_hard, 0.0, trans[:, -1])

        cloud_first = has_weak & (first_weak < first_hard)
        idx_cloud = np.minimum(first_weak, len(s) - 1)
        idx_hard = np.minimum(first_hard, len(s) - 1)
        terrain_at_hit = path.terrain_km[rows, idx_hard]
        hard_cause = np.where((terrain_at_hit > _SEA_LEVEL_KM) & (z[idx_hard] <= terrain_at_hit), TERRAIN, HORIZON)

        block_cause[:, a] = np.where(cloud_first, layer[idx_cloud], np.where(has_hard, hard_cause, NONE))
        block_km[:, a] = np.where(cloud_first, s[idx_cloud], np.where(has_hard, s[idx_hard], 0.0))

    return {"T": T, "block_km": block_km, "block_cause": block_cause, "clear": clear}


def path_transmission(rays, params):
    """Alpha-weighted transmission relative to a cloud-free path over a smooth earth (0–1)."""
    _, weights = _alphas(params)
    denom = np.sum(weights * rays["clear"])
    if denom <= 0:
        return np.zeros(rays["T"].shape[0])
    return np.clip((rays["T"] * weights).sum(axis=1) / denom, 0.0, 1.0)


def canopy(cover, params):
    """Usefulness of a cloud layer as a screen: none without cloud, best when broken, reduced when overcast."""
    p = params["canopy"]
    c = np.clip(cover, 0.0, 1.0)
    rise = np.clip(c / p["rise"], 0.0, 1.0)
    fall = 1.0 - (1.0 - p["full_sky_factor"]) * np.clip((c - p["fall"]) / (1.0 - p["fall"]), 0.0, 1.0)
    return rise * fall


def observation(low, elev_m, params):
    """Visibility of the lit canopy: local low cloud blocks the view, unless the observer is above it."""
    p = params["observation"]
    sea = p["sea_of_clouds_elev_m"]
    weight = np.clip(1.0 - np.asarray(elev_m) / sea, 0.0, 1.0)
    bonus = np.where(np.asarray(elev_m) >= sea, p["sea_bonus"], 0.0)
    return 1.0 - p["k_low"] * low * weight + bonus * low


def evaluate(local, path, params):
    """Score points and return the components needed for maps and point details."""
    cp = params["canopy"]
    rays_mid = trace_rays(path, cp["mid_height_km"], params)
    rays_high = trace_rays(path, cp["high_height_km"], params)
    t_mid = path_transmission(rays_mid, params)
    t_high = path_transmission(rays_high, params)
    can_mid = canopy(local.mid, params)
    can_high = canopy(local.high, params)
    vis = observation(local.low, local.elev_m, params)
    raw = 1.0 - (1.0 - cp["weight_mid"] * can_mid * t_mid) * (1.0 - cp["weight_high"] * can_high * t_high)
    score = 100.0 * np.clip(raw * vis, 0.0, 1.0) ** params["gamma"]
    return {
        "score": score,
        "canopy_mid": can_mid,
        "canopy_high": can_high,
        "trans_mid": t_mid,
        "trans_high": t_high,
        "vis": vis,
        "rays_mid": rays_mid,
        "rays_high": rays_high,
    }


# One-line verdict reasons, rendered as text by the frontend
NO_CANOPY, LOCAL_LOW, BLOCKED, PATH_DIM, OVERCAST, OPEN = 1, 2, 3, 4, 5, 6
REASON_NAMES = {NO_CANOPY: "no_canopy", LOCAL_LOW: "local_low", BLOCKED: "blocked",
                PATH_DIM: "path_dim", OVERCAST: "overcast", OPEN: "open"}


def explain(res, local, params):
    """Main reason behind each score, plus the blocking cause and distance when light was cut off.

    The canopy layer that contributes more is examined; among its rays that clear the horizon, the first
    one (highest sun) stopped by cloud or terrain is the reported block, since it cuts the glow earliest.
    """
    cp = params["canopy"]
    use_mid = cp["weight_mid"] * res["canopy_mid"] >= cp["weight_high"] * res["canopy_high"]
    trans = np.where(use_mid, res["trans_mid"], res["trans_high"])
    cover = np.where(use_mid, local.mid, local.high)
    n = len(trans)
    cause = np.zeros(n, dtype=np.int8)
    km = np.zeros(n)
    for rays, sel in ((res["rays_mid"], use_mid), (res["rays_high"], ~use_mid)):
        for a in range(rays["T"].shape[1]):
            if not rays["clear"][a]:
                continue
            c = rays["block_cause"][:, a]
            hit = sel & (cause == NONE) & np.isin(c, (LOW, MID, HIGH, TERRAIN))
            cause = np.where(hit, c, cause)
            km = np.where(hit, rays["block_km"][:, a], km)

    code = np.full(n, OPEN, dtype=np.int8)
    code = np.where(cover > cp["fall"], OVERCAST, code)
    code = np.where(trans < 0.5, PATH_DIM, code)
    code = np.where((cause != NONE) & (trans < 0.75), BLOCKED, code)
    code = np.where(res["vis"] < 0.5, LOCAL_LOW, code)
    code = np.where(np.maximum(res["canopy_mid"], res["canopy_high"]) < 0.2, NO_CANOPY, code)
    return {"code": code, "cause": cause, "km": km, "layer": np.where(use_mid, MID, HIGH)}
