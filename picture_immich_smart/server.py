"""picture_immich_smart, full-bleed Immich photos framed around their subject.

Picks a random photo (from an album or the whole library) and frames it to
the cell's aspect ratio without hand-cropping:

* **Faces first.** Immich's face detection already stores a bounding box
  per face. The crop window is placed so every face fits, with headroom
  above, and the faces sit on the upper third of the frame.
* **Saliency otherwise.** With no faces, a small Immich thumbnail is scored
  for edge detail and colour, and the window slides to the busiest region.
* **When the faces can't fit** (a tall group shot in a wide frame), the
  ``fallback`` option decides: letterbox on a blurred copy of the photo,
  skip to another photo, or crop around the largest face anyway.

The crop is expressed as CSS ``object-position`` percentages, so the photo
itself is never re-encoded here. Connections are shared with the
``picture_immich`` widget (Widgets -> Immich): this widget reads its saved
libraries and serves images through its key-hiding proxy, so there is no
second place to paste an API key.
"""

from __future__ import annotations

import io
import json
import logging
import random
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_PLUGIN = "picture_immich"
HTTP_TIMEOUT_S = 12
RANDOM_BATCH = 24
# Aspect ratios within this factor of each other need no crop decision.
ASPECT_TOLERANCE = 1.03

# Padding around the union of face boxes, as fractions of the union's size.
FACE_PAD_SIDE = 0.25
FACE_PAD_TOP = 0.45  # headroom: hair and a little air above the head
FACE_PAD_BOTTOM = 0.35  # chin and a hint of shoulders
# Where the faces' centre should sit in the window (rule of thirds).
FACE_TARGET_Y = 0.38
# Faces smaller than this fraction of the largest face's area are
# background people (or false positives) and don't steer the crop.
MIN_FACE_RATIO = 0.25

SALIENCY_EDGE = 140  # long edge of the thumbnail scored for saliency
SALIENCY_CENTRE_BIAS = 0.15


# ----- pure framing logic (no Flask, no network) ----------------------


def iter_faces(asset: dict[str, Any]) -> list[dict[str, Any]]:
    """Every face box on the asset, named people and unassigned alike."""
    out: list[dict[str, Any]] = []
    for person in asset.get("people") or []:
        if isinstance(person, dict) and not person.get("isHidden"):
            out.extend(f for f in person.get("faces") or [] if isinstance(f, dict))
    out.extend(f for f in asset.get("unassignedFaces") or [] if isinstance(f, dict))
    return out


def _positive(*vals: Any) -> bool:
    return all(isinstance(v, (int, float)) and v > 0 for v in vals)


def oriented_size(asset: dict[str, Any]) -> tuple[int, int] | None:
    """(w, h) of the photo as displayed, from face metadata or EXIF.

    Face boxes are measured on the oriented preview, so their imageWidth /
    imageHeight are the most reliable. EXIF dims are pre-rotation, so
    orientations 5-8 (90/270 degree turns) swap them."""
    for face in iter_faces(asset):
        w, h = face.get("imageWidth"), face.get("imageHeight")
        if _positive(w, h):
            return int(w), int(h)
    exif = asset.get("exifInfo") or {}
    w, h = exif.get("exifImageWidth"), exif.get("exifImageHeight")
    if not _positive(w, h):
        return None
    try:
        orientation = int(exif.get("orientation") or 1)
    except (TypeError, ValueError):
        orientation = 1
    return (int(h), int(w)) if orientation in (5, 6, 7, 8) else (int(w), int(h))


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def face_boxes(asset: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    """Normalised (x1, y1, x2, y2) boxes in [0, 1], minor faces dropped."""
    boxes: list[tuple[float, float, float, float]] = []
    for f in iter_faces(asset):
        try:
            iw, ih = float(f["imageWidth"]), float(f["imageHeight"])
            x1, x2 = float(f["boundingBoxX1"]) / iw, float(f["boundingBoxX2"]) / iw
            y1, y2 = float(f["boundingBoxY1"]) / ih, float(f["boundingBoxY2"]) / ih
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        x1, x2 = sorted((_clamp(x1), _clamp(x2)))
        y1, y2 = sorted((_clamp(y1), _clamp(y2)))
        if x2 > x1 and y2 > y1:
            boxes.append((x1, y1, x2, y2))
    if not boxes:
        return []
    largest = max((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    return [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= largest * MIN_FACE_RATIO]


def crop_window(image_aspect: float, target_aspect: float) -> tuple[str | None, float]:
    """Which axis a cover-fit crops, and the visible fraction along it.

    Returns ("x", w_frac) for a photo wider than the frame, ("y", h_frac)
    for a taller one, or (None, 1.0) when the aspects already match."""
    ratio = image_aspect / target_aspect
    if 1 / ASPECT_TOLERANCE <= ratio <= ASPECT_TOLERANCE:
        return None, 1.0
    if ratio > 1:
        return "x", target_aspect / image_aspect
    return "y", image_aspect / target_aspect


def object_position(start: float, frac: float) -> float:
    """CSS object-position percentage that shows [start, start + frac]."""
    slack = 1.0 - frac
    return 50.0 if slack <= 1e-6 else round(100.0 * _clamp(start / slack), 2)


def place_window(lo: float, hi: float, frac: float, want: float) -> tuple[float, bool]:
    """Start of a frac-long window containing [lo, hi], nearest to want.

    Returns (start, fits). When [lo, hi] is longer than the window, the
    window is centred on it and fits is False."""
    if hi - lo > frac + 1e-6:
        return _clamp((lo + hi) / 2 - frac / 2, 0.0, 1.0 - frac), False
    start = _clamp(want, max(0.0, hi - frac), min(lo, 1.0 - frac))
    return _clamp(start, 0.0, 1.0 - frac), True


def _plan(fit: str, pos_x: float, pos_y: float, fits: bool, axis: str | None) -> dict[str, Any]:
    return {"fit": fit, "pos_x": pos_x, "pos_y": pos_y, "fits": fits, "axis": axis}


def frame_faces(
    boxes: list[tuple[float, float, float, float]],
    image_aspect: float,
    target_aspect: float,
) -> dict[str, Any]:
    """Frame a photo around its faces. Keys: fit ("cover"|"contain"),
    pos_x, pos_y (CSS %), fits (bool), axis."""
    axis, frac = crop_window(image_aspect, target_aspect)
    if axis is None:
        return _plan("cover", 50.0, 50.0, True, None)
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes)
    y2 = max(b[3] for b in boxes)
    fw, fh = x2 - x1, y2 - y1
    if axis == "x":
        want = (x1 + x2) / 2 - frac / 2
        lo, hi = _clamp(x1 - fw * FACE_PAD_SIDE), _clamp(x2 + fw * FACE_PAD_SIDE)
        start, fits = place_window(lo, hi, frac, want)
        if not fits:  # padding is a nicety; the bare faces may still fit
            start, fits = place_window(x1, x2, frac, want)
        return _plan("cover", object_position(start, frac), 50.0, fits, axis)
    want = (y1 + y2) / 2 - frac * FACE_TARGET_Y
    lo, hi = _clamp(y1 - fh * FACE_PAD_TOP), _clamp(y2 + fh * FACE_PAD_BOTTOM)
    start, fits = place_window(lo, hi, frac, want)
    if not fits:
        start, fits = place_window(y1, y2, frac, want)
    return _plan("cover", 50.0, object_position(start, frac), fits, axis)


def largest_face_frame(
    boxes: list[tuple[float, float, float, float]], image_aspect: float, target_aspect: float
) -> dict[str, Any]:
    biggest = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return frame_faces([biggest], image_aspect, target_aspect)


def best_window(profile: list[float], frac: float) -> float:
    """Start (0..1) of the frac-long window with the most mass in profile,
    with a mild pull toward the centre to break near-ties."""
    n = len(profile)
    win = max(1, round(frac * n))
    if n == 0 or win >= n:
        return 0.0
    total = sum(profile) or 1.0
    run = sum(profile[:win])
    centre = (n - win) / 2
    best_score, best_i = float("-inf"), 0
    for i in range(n - win + 1):
        if i:
            run += profile[i + win - 1] - profile[i - 1]
        score = run / total - SALIENCY_CENTRE_BIAS * abs(i - centre) / (n - win)
        if score > best_score:
            best_score, best_i = score, i
    return best_i / n


def saliency_profiles(img: Any) -> tuple[list[float], list[float]]:
    """Column and row sums of an edge + colourfulness energy map of a PIL
    image (numpy ships with Tesserae)."""
    import numpy as np
    from PIL import ImageFilter

    im = img.convert("RGB")
    im.thumbnail((SALIENCY_EDGE, SALIENCY_EDGE))
    edges = np.asarray(im.convert("L").filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    sat = np.asarray(im.convert("HSV").getchannel("S"), dtype=np.float32)
    energy = edges + 0.5 * sat
    # FIND_EDGES leaves the 1 px border bright; ignore it.
    energy[0, :] = energy[-1, :] = 0
    energy[:, 0] = energy[:, -1] = 0
    return energy.sum(axis=0).tolist(), energy.sum(axis=1).tolist()


def frame_saliency(
    cols: list[float], rows: list[float], image_aspect: float, target_aspect: float
) -> dict[str, Any]:
    axis, frac = crop_window(image_aspect, target_aspect)
    if axis is None:
        return _plan("cover", 50.0, 50.0, True, None)
    if axis == "x":
        return _plan("cover", object_position(best_window(cols, frac), frac), 50.0, True, axis)
    return _plan("cover", 50.0, object_position(best_window(rows, frac), frac), True, axis)


def plan_asset(
    asset: dict[str, Any], target_aspect: float, fallback: str, thumb_loader: Any
) -> dict[str, Any] | None:
    """Framing for one asset, or None to skip it (fallback "skip").

    thumb_loader(asset_id) returns a PIL image or None; it is only called
    when there are no faces to frame on (or no size metadata)."""
    centre = dict(_plan("cover", 50.0, 50.0, True, None), focus="centre", faces=0)
    boxes = face_boxes(asset)
    size = oriented_size(asset)
    thumb = None
    if size is None:
        thumb = thumb_loader(asset["id"])
        if thumb is None:
            return centre
        size = thumb.size
    aspect = size[0] / size[1]
    if boxes:
        plan = dict(frame_faces(boxes, aspect, target_aspect), focus="faces", faces=len(boxes))
        if plan["fits"]:
            return plan
        if fallback == "skip":
            return None
        if fallback == "crop":
            return dict(largest_face_frame(boxes, aspect, target_aspect),
                        focus="largest_face", faces=len(boxes))
        return dict(plan, fit="contain", pos_x=50.0, pos_y=50.0, focus="letterbox")
    if crop_window(aspect, target_aspect)[0] is None:
        return dict(centre, focus="none_needed")
    thumb = thumb or thumb_loader(asset["id"])
    if thumb is None:
        return centre
    cols, rows = saliency_profiles(thumb)
    return dict(frame_saliency(cols, rows, aspect, target_aspect), focus="saliency", faces=0)


def matches_orientation(asset: dict[str, Any], target_aspect: float) -> bool:
    size = oriented_size(asset)
    if size is None:
        return True
    return (size[0] >= size[1]) == (target_aspect >= 1)


# ----- Tesserae / Immich glue ----------------------------------------


def _source_plugin() -> Any:
    from flask import current_app

    return current_app.config["PLUGIN_REGISTRY"].get(SOURCE_PLUGIN)


def _library(library_id: str) -> tuple[dict[str, Any] | None, str]:
    """(library record, plaintext API key) from picture_immich's store."""
    from flask import current_app

    plugin = _source_plugin()
    if plugin is None:
        return None, ""
    try:
        libraries = json.loads((Path(plugin.data_dir) / "libraries.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(libraries, list):
        return None, ""
    lib = next((x for x in libraries if isinstance(x, dict) and x.get("id") == library_id), None)
    if lib is None:
        return None, ""
    stored = lib.get("api_key_secret") or ""
    box = current_app.config.get("SECRET_BOX")
    if box is None or not stored:
        return lib, stored
    try:
        return lib, str(box.unwrap(stored))
    except Exception:  # noqa: BLE001  an undecryptable key reads as "no key"
        return lib, ""


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "Accept": "application/json",
            "User-Agent": "tesserae-picture_immich_smart/0.1"}


def _random_assets(base: str, key: str, album_id: str | None) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"size": RANDOM_BATCH, "type": "IMAGE",
                            "withPeople": True, "withExif": True}
    if album_id:
        body["albumIds"] = [album_id]
    req = urllib.request.Request(
        f"{base}/api/search/random",
        data=json.dumps(body).encode("utf-8"),
        headers=dict(_headers(key), **{"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.info("picture_immich_smart: search/random failed: %s", exc)
        return []
    if not isinstance(payload, list):
        return []
    return [a for a in payload if isinstance(a, dict) and a.get("id")]


def _thumbnail(base: str, key: str, asset_id: str) -> Any:
    from PIL import Image, ImageOps

    url = f"{base}/api/assets/{urllib.parse.quote(asset_id, safe='')}/thumbnail?size=thumbnail"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=_headers(key)), timeout=HTTP_TIMEOUT_S
        ) as resp:
            im = Image.open(io.BytesIO(resp.read()))
            im.load()
        return ImageOps.exif_transpose(im) or im
    except Exception as exc:  # noqa: BLE001
        logger.info("picture_immich_smart: thumbnail failed for %s: %s", asset_id, exc)
        return None


# ----- plugin contract ------------------------------------------------


def choices(name: str) -> list[dict[str, Any]]:
    """Library / album pickers, delegated to picture_immich so both widgets
    list exactly the same connections."""
    try:
        plugin = _source_plugin()
        if plugin is None:
            return [{"value": "", "label": "Install the Picture, Immich widget first"}]
        return plugin.server_module.choices(name)
    except Exception as exc:  # noqa: BLE001
        logger.info("picture_immich_smart: choices(%s) failed: %s", name, exc)
        return [{"value": "", "label": "Immich libraries unavailable"}]


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    try:
        return _fetch(options, ctx)
    except Exception as exc:  # noqa: BLE001  never take the cell down
        logger.exception("picture_immich_smart: fetch failed")
        return {"error": f"Immich photo unavailable ({type(exc).__name__})."}


def _fetch(options: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    library_id = (options.get("library_id") or "").strip()
    if not library_id:
        return {"error": "Pick an Immich library in the cell editor."}
    if _source_plugin() is None:
        return {"error": "This widget uses the Picture, Immich widget's connections; install it first."}
    lib, key = _library(library_id)
    if lib is None:
        return {"error": f"Immich library {library_id!r} not found."}
    base = (lib.get("url") or "").rstrip("/")
    if not (base and key):
        return {"error": "The Immich library has no URL or API key."}

    mode = options.get("mode") or "random_album"
    album_id = (options.get("album_id") or "").strip() if mode == "random_album" else None
    if mode == "random_album" and not album_id:
        return {"error": "Pick an album in the cell editor."}
    cw = float(ctx.get("cell_w") or ctx.get("panel_w") or 1600)
    ch = float(ctx.get("cell_h") or ctx.get("panel_h") or 1200)
    target = cw / ch if ch > 0 else 4 / 3
    fallback = options.get("fallback") or "blur"

    assets = _random_assets(base, key, album_id)
    if not assets:
        return {"error": "Immich returned no photos. Check the album and connection."}
    random.shuffle(assets)  # noqa: S311  display-only
    pool = assets
    if (options.get("orientation") or "any") == "match":
        # Never blank the frame over a filter: fall back to everything.
        pool = [a for a in assets if matches_orientation(a, target)] or assets

    thumbs: dict[str, Any] = {}

    def loader(asset_id: str) -> Any:
        if asset_id not in thumbs:
            thumbs[asset_id] = _thumbnail(base, key, asset_id)
        return thumbs[asset_id]

    chosen, plan = pool[0], None
    for asset in pool:
        plan = plan_asset(asset, target, fallback, loader)
        if plan is not None:
            chosen = asset
            break
    if plan is None:  # every candidate skipped: letterbox the first
        plan = plan_asset(chosen, target, "blur", loader)

    safe_lib = urllib.parse.quote(library_id, safe="")
    safe_asset = urllib.parse.quote(chosen["id"], safe="")
    return {
        "asset_id": chosen["id"],
        "taken_at": chosen.get("localDateTime") or chosen.get("fileCreatedAt"),
        "city": (chosen.get("exifInfo") or {}).get("city"),
        # picture_immich's proxy keeps the API key server-side.
        "image_url": f"/plugins/{SOURCE_PLUGIN}/image/{safe_lib}/{safe_asset}",
        "fit": plan["fit"],
        "pos_x": plan["pos_x"],
        "pos_y": plan["pos_y"],
        "focus": plan.get("focus"),
        "faces": plan.get("faces", 0),
        "show_caption": bool(options.get("show_caption", False)),
    }
