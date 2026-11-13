import eccodes
import numpy as np


def split_messages(data):
    """Split concatenated GRIB2 messages; the total length is a big-endian uint64 at byte 8."""
    out = []
    pos = data.find(b"GRIB")
    while pos >= 0 and pos + 16 <= len(data):
        length = int.from_bytes(data[pos + 8:pos + 16], "big")
        out.append(data[pos:pos + length])
        pos = data.find(b"GRIB", pos + length)
    return out


def decode(data, keys=("shortName", "typeOfFirstFixedSurface", "stepType")):
    """Decode GRIB bytes into dicts holding the requested keys plus ascending lats, lons and a 2-D values array."""
    fields = []
    for msg in split_messages(data):
        h = eccodes.codes_new_from_message(msg)
        try:
            info = {k: eccodes.codes_get(h, k, ktype=str) for k in keys}
            ni = eccodes.codes_get(h, "Ni")
            nj = eccodes.codes_get(h, "Nj")
            values = eccodes.codes_get_values(h).reshape(nj, ni)
            lats = eccodes.codes_get_array(h, "latitudes").reshape(nj, ni)[:, 0]
            lons = eccodes.codes_get_array(h, "longitudes").reshape(nj, ni)[0, :]
        finally:
            eccodes.codes_release(h)
        lons = np.where(lons > 180, lons - 360, lons)
        jo = np.argsort(lats)
        io = np.argsort(lons)
        info.update(lats=lats[jo], lons=lons[io], values=values[np.ix_(jo, io)])
        fields.append(info)
    return fields


def crop(lats, lons, values, box, margin=0.0):
    """Cut a regular ascending grid down to the box (plus margin) so later bilinear lookups stay inside it."""
    jm = (lats >= box.lat_min - margin) & (lats <= box.lat_max + margin)
    im = (lons >= box.lon_min - margin) & (lons <= box.lon_max + margin)
    return lats[jm], lons[im], values[np.ix_(jm, im)]
