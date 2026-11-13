import numpy as np
import pytest

from glow.geometry import EARTH_RADIUS_KM, bilinear, destination, ray_height


def exact_ray_height(h, alpha, s, r):
    """Height above a sphere of radius r of a straight ray through height h at elevation angle alpha,
    at arc distance s along the surface (solved by ray–circle geometry, no small-angle approximation)."""
    # Place the start point at (0, r + h); the ray direction is (cos alpha, sin alpha).
    # For a surface angle theta = s / r, the ray meets the radial line at angle theta where
    # the point is (rho sin theta, rho cos theta) and lies on the ray.
    theta = s / r
    # Solve rho * cos(theta) = r + h + d sin(alpha), rho * sin(theta) = d cos(alpha) for rho.
    rho = (r + h) * np.cos(alpha) / np.cos(theta + alpha)
    return rho - r


@pytest.mark.parametrize("alpha_deg", [0.5, 0.0, -0.5, -1.0, -2.0, -3.0])
@pytest.mark.parametrize("h", [4.5, 9.0])
def test_ray_height_matches_spherical_geometry(alpha_deg, h):
    r = 8000.0
    s = np.linspace(0, 500, 51)
    alpha = np.radians(alpha_deg)
    approx = ray_height(h, alpha, s, r)
    exact = exact_ray_height(h, alpha, s, r)
    # The parabolic form drops terms of order h*s/r, alpha^2 * s^2 / r and s^4 / r^3. Where the ray
    # can still meet weather (below 12 km) that stays under 60 m, far finer than the layer boundaries.
    relevant = exact < 12.0
    assert np.max(np.abs(approx - exact)[relevant]) < 0.06


def test_ray_height_analytic_values():
    # z(s) = h + alpha s + s^2 / (2R)
    assert ray_height(4.5, 0.0, 400.0, 8000.0) == pytest.approx(4.5 + 10.0)
    assert ray_height(4.5, -0.01, 100.0, 8000.0) == pytest.approx(4.5 - 1.0 + 0.625)
    # Lowest point of the ray sits at s = -alpha R
    s = np.linspace(0, 500, 5001)
    z = ray_height(4.5, -0.02, s, 8000.0)
    assert s[np.argmin(z)] == pytest.approx(160.0, abs=0.2)


def test_destination_distance_and_bearing():
    lat, lon = 35.4, 139.6
    for az in (60.0, 120.0, 240.0, 300.0):
        lat2, lon2 = destination(lat, lon, az, 300.0)
        p1, p2 = np.radians([lat, lat2])
        dl = np.radians(lon2 - lon)
        hav = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
        assert 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(hav)) == pytest.approx(300.0, abs=1e-6)
        bearing = np.degrees(np.arctan2(np.sin(dl) * np.cos(p2),
                                        np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)))
        assert bearing % 360 == pytest.approx(az, abs=1e-6)


def test_bilinear_reproduces_linear_field():
    lats = np.arange(30.0, 40.01, 0.25)
    lons = np.arange(130.0, 150.01, 0.25)
    field = 2 * lats[:, None] + 3 * lons[None, :]
    q_lat = np.array([33.1, 35.77, 39.9])
    q_lon = np.array([134.6, 139.33, 145.01])
    assert bilinear(field, lats, lons, q_lat, q_lon) == pytest.approx(2 * q_lat + 3 * q_lon)
