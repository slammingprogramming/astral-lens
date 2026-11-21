"""Region-level geolocation from the look of an image (GeoCLIP). A guess, never a location.

Scope and limits, deliberately plain:
- The model matches an image to a gallery of 100,000 GPS coordinates by appearance. It has no idea where a photograph was
  taken; it says which places in its training photographs the picture resembles. Those photographs are tourist and landmark
  heavy, so well-photographed places are favoured and ordinary streets are not understood.
- The output is therefore REGIONS, never points: the model's top predictions are pooled into coarse grid cells (5 degrees by
  default, roughly 550 km), each with the share of the model's score that fell in it. The share is the model's softmax mass,
  NOT a calibrated probability, and it says nothing about whether the picture was taken there. The best region is wrong
  often; the model's own paper reports country-level accuracy well below one in two on its benchmark.
- Nothing here reads EXIF GPS or any other claim by the file (media.py does, and records it as a claim). The two are separate
  sources and should be compared by a person, not merged.
- The weights' licence is not stated by their authors and the training photographs are a Flickr-derived set with mixed
  licences, so the model is registered as an unknown licence and is used only on a knowing opt-in (`allow_unverified_licence`).
- Nothing is downloaded at run time: the CLIP image backbone must already be on disk (ASTRAL_GEOCLIP_CLIP_DIR).
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Any

from astral.backends import module_available

CELL_DEG = 5.0
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def clip_dir() -> Path | None:
    d = os.environ.get("ASTRAL_GEOCLIP_CLIP_DIR")
    return Path(d) if d and (Path(d) / "config.json").exists() else None


def regionize(coords: list[tuple[float, float]], probs: list[float], *, cell: float = CELL_DEG, max_regions: int = 5) -> dict[str, Any]:
    """Pool ranked GPS predictions into grid cells. Returns the best `max_regions` cells by pooled score, and how diffuse the
    whole answer is. A cell is a bounding box; no point inside it is more likely than another."""
    if not coords or len(coords) != len(probs):
        return {"regions": [], "total_mass": 0.0, "diffuse": True}
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    for (lat, lon), p in zip(coords, probs):
        lat = max(-90.0, min(90.0, float(lat)))
        lon = ((float(lon) + 180.0) % 360.0) - 180.0
        key = (min(int(math.floor((lat + 90.0) / cell)), int(180 / cell) - 1), int(math.floor((lon + 180.0) / cell)))
        c = cells.setdefault(key, {"mass": 0.0, "n": 0})
        c["mass"] += float(p)
        c["n"] += 1
    total = sum(c["mass"] for c in cells.values())
    ranked = sorted(cells.items(), key=lambda kv: -kv[1]["mass"])[:max_regions]
    regions = []
    for rank, ((i, j), c) in enumerate(ranked, 1):
        south, west = -90.0 + i * cell, -180.0 + j * cell
        regions.append({"rank": rank, "south": south, "north": south + cell, "west": west, "east": west + cell,
                        "mass": round(c["mass"], 4), "share_of_returned": round(c["mass"] / total, 3) if total else 0.0,
                        "predictions": c["n"]})
    top = regions[0]["share_of_returned"] if regions else 0.0
    return {"regions": regions, "total_mass": round(total, 4), "cell_degrees": cell,
            "diffuse": top < 0.4}                                  # the best cell holds under 40% of what the model returned


class GeoClipBackend:
    """GeoCLIP through PyTorch on the CPU. Unknown-licence weights: knowing opt-in only. NOT in the default environment:
    it needs `torch`, `transformers` and `geoclip`, which are large and optional."""

    name, capability = "geoclip", "geo.region"
    model_id = "geoclip"
    detector_id = "geoclip"

    def available(self) -> tuple[bool, str]:
        for mod in ("torch", "transformers", "geoclip"):
            if not module_available(mod):
                return False, f"{mod} is not installed (an optional dependency: pip install geoclip, with PyTorch)"
        if clip_dir() is None:
            return False, "ASTRAL_GEOCLIP_CLIP_DIR is not set to a local copy of openai/clip-vit-large-patch14 (nothing is downloaded at run time)"
        return True, ""

    def supports(self, allow_unverified_licence: bool = False, **_: Any) -> bool:
        return bool(allow_unverified_licence)

    def _model(self):
        with _lock:
            if "m" not in _cache:
                import torch
                from geoclip import GeoCLIP

                torch.set_num_threads(int(os.environ.get("ASTRAL_GEOCLIP_THREADS", "4")))
                _cache["m"] = GeoCLIP(from_pretrained=True, clip_path=str(clip_dir()))
                _cache["m"].eval()
        return _cache["m"]

    def run(self, *, data: bytes, top_k: int = 100, cell_degrees: float = CELL_DEG, max_regions: int = 5, **_: Any) -> dict[str, Any]:
        import io

        from PIL import Image

        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:                                       # noqa: BLE001
            raise ValueError("not a decodable image") from e
        model = self._model()
        import torch

        with _lock, torch.no_grad():
            x = model.image_encoder.preprocess_image(img)
            logits = model(x, model.gps_gallery)                      # (1, gallery) similarity to every gallery coordinate
            probs = logits.softmax(dim=-1)[0]
            top = probs.topk(max(1, int(top_k)))
        coords = [(float(model.gps_gallery[i][0]), float(model.gps_gallery[i][1])) for i in top.indices.tolist()]
        out = regionize(coords, [float(p) for p in top.values.tolist()], cell=cell_degrees, max_regions=max_regions)
        out["top_k"] = int(top_k)
        out["gallery"] = int(model.gps_gallery.shape[0])
        out["image"] = {"width": img.size[0], "height": img.size[1]}
        return out


def register_geo_backends(reg) -> None:
    reg.register(GeoClipBackend())
