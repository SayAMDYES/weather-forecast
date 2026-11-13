import numpy as np

EARTH_RADIUS_KM = 6371.0


def ray_height(h_km, alpha_rad, s_km, r_eff_km):
    """Height of the ray that lights a cloud at height h above the observer, at distance s toward the sun.

    alpha is the solar elevation (negative below the horizon); the s^2/(2R) term is the curvature drop
    of the surface under a straight ray, with R enlarged to account for atmospheric refraction.
    """
    return h_km + alpha_rad * s_km + s_km * s_km / (2.0 * r_eff_km)


def destination(lat_deg, lon_deg, azimuth_deg, dist_km):
    """Great-circle destination points; inputs broadcast against each other."""
    lat1 = np.radians(lat_deg)
    lon1 = np.radians(lon_deg)
    az = np.radians(azimuth_deg)
    d = np.asarray(dist_km) / EARTH_RADIUS_KM
    sin_lat2 = np.sin(lat1) * np.cos(d) + np.cos(lat1) * np.sin(d) * np.cos(az)
    lat2 = np.arcsin(sin_lat2)
    lon2 = lon1 + np.arctan2(np.sin(az) * np.sin(d) * np.cos(lat1), np.cos(d) - np.sin(lat1) * sin_lat2)
    return np.degrees(lat2), np.degrees(lon2)


def bilinear(field, lats, lons, lat_q, lon_q):
    """Bilinear interpolation on a regular grid with ascending lats/lons; queries outside are clamped."""
    lat0, dlat = lats[0], lats[1] - lats[0]
    lon0, dlon = lons[0], lons[1] - lons[0]
    fy = np.clip((np.asarray(lat_q) - lat0) / dlat, 0, len(lats) - 1.000001)
    fx = np.clip((np.asarray(lon_q) - lon0) / dlon, 0, len(lons) - 1.000001)
    j = fy.astype(np.intp)
    i = fx.astype(np.intp)
    ty = fy - j
    tx = fx - i
    return (field[j, i] * (1 - tx) * (1 - ty) + field[j, i + 1] * tx * (1 - ty)
            + field[j + 1, i] * (1 - tx) * ty + field[j + 1, i + 1] * tx * ty)
