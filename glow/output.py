import json
import os
from pathlib import Path

import numpy as np
from PIL import Image

# Order of the 8-bit layers stacked top to bottom in each grid PNG; each holds a 0–100 value.
GRID_LAYERS = ["index", "low", "mid", "high"]


def write_grid_png(path, layers, ny, nx):
    """Stack 0–100 layers (rows north→south) vertically into one grayscale PNG."""
    stack = np.concatenate([np.clip(np.rint(layers[name].reshape(ny, nx)), 0, 100) for name in GRID_LAYERS])
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(stack.astype(np.uint8), mode="L").save(path, optimize=True)


def write_json(path, obj):
    """Write via a temp file and rename, so the server never serves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def remove_stale(out_dir, keep):
    """Delete generated files of earlier runs that the new manifest no longer references."""
    for sub in ("grid", "sun"):
        for f in (out_dir / sub).glob("*"):
            if f"{sub}/{f.name}" not in keep:
                f.unlink()
