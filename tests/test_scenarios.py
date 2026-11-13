"""Synthetic sunset scenarios: one observer, cloud and terrain profiles toward the sun."""
import numpy as np
import pytest

from glow.config import load_config
from glow.score import HORIZON, LOW, TERRAIN, Local, Path, evaluate, sample_distances

PARAMS = load_config()["scoring"]
S = sample_distances(PARAMS)


def scene(local_low=0.0, local_mid=0.0, local_high=0.0, elev_m=0.0, low=None, mid=None, high=None, terrain=None):
    zeros = np.zeros_like(S)
    path = Path(
        low=(zeros if low is None else low)[None, :],
        mid=(zeros if mid is None else mid)[None, :],
        high=(zeros if high is None else high)[None, :],
        terrain_km=(zeros if terrain is None else terrain)[None, :],
    )
    local = Local(np.array([local_low]), np.array([local_mid]), np.array([local_high]), np.array([elev_m]))
    return evaluate(local, path, PARAMS)


def only_near(value, radius_km=30.0):
    return np.where(S <= radius_km, value, 0.0)


def between(value, s0, s1):
    return np.where((S >= s0) & (S <= s1), value, 0.0)


BASE = dict(local_mid=0.5, mid=only_near(0.5))


def test_clear_sky_scores_low():
    assert scene()["score"][0] < 5


def test_local_mid_cloud_with_open_path_scores_high():
    assert scene(**BASE)["score"][0] >= 70


def test_low_cloud_wall_toward_sun_lowers_score():
    base = scene(**BASE)["score"][0]
    res = scene(**BASE, low=between(1.0, 150, 250))
    assert res["score"][0] <= base - 10
    # Some ray lighting the mid canopy is reported as stopped by low cloud inside the wall
    rays = res["rays_mid"]
    hit = rays["block_cause"][0] == LOW
    assert hit.any()
    assert np.all((rays["block_km"][0][hit] >= 150) & (rays["block_km"][0][hit] <= 250))


def test_mountain_toward_sun_lowers_score_and_reports_location():
    base = scene(**BASE)["score"][0]
    res = scene(**BASE, terrain=between(3.0, 60, 100))
    assert res["score"][0] < base - 5
    rays = res["rays_mid"]
    hit = rays["block_cause"][0] == TERRAIN
    assert hit.any()
    assert np.all((rays["block_km"][0][hit] >= 60) & (rays["block_km"][0][hit] <= 100))


def test_rays_below_horizon_are_reported_as_horizon():
    rays = scene(**BASE)["rays_mid"]
    assert (rays["block_cause"][0] == HORIZON).any()


def test_overcast_thick_low_cloud_scores_low():
    res = scene(local_low=1.0, local_mid=0.5, low=np.ones_like(S), mid=only_near(0.5))
    assert res["score"][0] < 20


def test_summit_above_low_cloud_is_not_penalised():
    base = scene(**BASE, elev_m=2000.0)["score"][0]
    res = scene(**BASE, elev_m=2000.0, local_low=1.0, low=only_near(1.0))
    assert res["score"][0] >= base - 1
    # The same low deck at sea level does hurt
    assert scene(**BASE, local_low=1.0, low=only_near(1.0))["score"][0] < base - 30


@pytest.mark.parametrize("cover,expected", [(0.0, 0.0), (0.15, 0.5), (0.5, 1.0), (1.0, 0.6)])
def test_canopy_shape(cover, expected):
    from glow.score import canopy
    assert canopy(np.array(cover), PARAMS) == pytest.approx(expected)


def _reason(**kw):
    from glow.score import explain
    res = scene(**kw)
    local = Local(np.array([kw.get("local_low", 0.0)]), np.array([kw.get("local_mid", 0.0)]),
                  np.array([kw.get("local_high", 0.0)]), np.array([kw.get("elev_m", 0.0)]))
    return explain(res, local, PARAMS)


def test_reasons():
    from glow.score import BLOCKED, LOCAL_LOW, NO_CANOPY, OPEN
    assert _reason()["code"][0] == NO_CANOPY
    assert _reason(**BASE)["code"][0] == OPEN
    wall = _reason(**BASE, low=between(1.0, 150, 250))
    assert wall["code"][0] == BLOCKED and wall["cause"][0] == LOW and 150 <= wall["km"][0] <= 250
    mtn = _reason(**BASE, terrain=between(3.0, 60, 100))
    assert mtn["code"][0] == BLOCKED and mtn["cause"][0] == TERRAIN and 60 <= mtn["km"][0] <= 100
    assert _reason(local_low=1.0, local_mid=0.5, low=np.ones_like(S), mid=only_near(0.5))["code"][0] == LOCAL_LOW
