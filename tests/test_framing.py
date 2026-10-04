"""Pure framing logic: no Tesserae, no Immich, no network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

_spec = importlib.util.spec_from_file_location(
    "smart", Path(__file__).parents[1] / "picture_immich_smart" / "server.py"
)
smart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smart)

LANDSCAPE = 4 / 3  # the 1600x1200 Inkplate 13


def face(x1, y1, x2, y2, w=1000, h=1500):
    return {"boundingBoxX1": x1, "boundingBoxY1": y1, "boundingBoxX2": x2,
            "boundingBoxY2": y2, "imageWidth": w, "imageHeight": h}


def asset(*faces, exif=None, aid="a1"):
    return {"id": aid, "people": [{"faces": list(faces)}], "exifInfo": exif or {}}


def visible(pos_pct, frac):
    """Inverse of object_position: the [start, end] window shown."""
    start = pos_pct / 100 * (1 - frac)
    return start, start + frac


def test_crop_window_axes():
    assert smart.crop_window(2 / 3, LANDSCAPE) == ("y", pytest.approx(0.5))
    axis, frac = smart.crop_window(16 / 9, LANDSCAPE)
    assert axis == "x" and frac == pytest.approx(0.75)
    assert smart.crop_window(1.34, LANDSCAPE)[0] is None  # within tolerance


def test_object_position_round_trip():
    assert smart.object_position(0.0, 0.5) == 0.0
    assert smart.object_position(0.5, 0.5) == 100.0
    assert smart.object_position(0.25, 0.5) == 50.0
    assert smart.object_position(0.3, 1.0) == 50.0


def test_portrait_face_near_top_is_kept_with_headroom():
    # Portrait 1000x1500, face high in the frame (y 10-25%). A centre crop
    # (25-75%) would cut it off; the smart crop must include it.
    a = asset(face(400, 150, 600, 375))
    plan = smart.plan_asset(a, LANDSCAPE, "blur", lambda _id: None)
    assert plan["focus"] == "faces" and plan["fit"] == "cover"
    top, bottom = visible(plan["pos_y"], 0.5)
    assert top <= 0.10 - 0.15 * 0.45 + 1e-6 or top == 0.0  # headroom kept
    assert bottom >= 0.25


def test_wide_photo_pans_to_face_on_the_right():
    a = asset(face(1500, 300, 1750, 600, w=2000, h=1000))  # 2:1 photo
    plan = smart.plan_asset(a, LANDSCAPE, "blur", lambda _id: None)
    frac = LANDSCAPE / 2.0
    left, right = visible(plan["pos_x"], frac)
    assert left <= 0.75 and right >= 0.875


def test_group_that_cannot_fit_letterboxes_skips_or_crops():
    # Faces span y 5%..95% of a tall photo: no 50%-tall window holds them.
    a = asset(face(100, 75, 300, 300), face(600, 1200, 800, 1425))
    loader = lambda _id: None  # noqa: E731
    assert smart.plan_asset(a, LANDSCAPE, "blur", loader)["fit"] == "contain"
    assert smart.plan_asset(a, LANDSCAPE, "skip", loader) is None
    crop = smart.plan_asset(a, LANDSCAPE, "crop", loader)
    assert crop["fit"] == "cover" and crop["focus"] == "largest_face"


def test_tiny_background_faces_do_not_steer():
    big = face(400, 900, 700, 1250)
    tiny = face(50, 20, 80, 60)  # far away, top-left
    boxes = smart.face_boxes(asset(big, tiny))
    assert len(boxes) == 1


def test_hidden_people_and_unassigned_faces():
    a = {"id": "x", "people": [{"isHidden": True, "faces": [face(0, 0, 10, 10)]}],
         "unassignedFaces": [face(400, 600, 600, 800)]}
    assert len(smart.face_boxes(a)) == 1


def test_oriented_size_swaps_for_rotated_exif():
    a = {"id": "x", "exifInfo": {"exifImageWidth": 4000, "exifImageHeight": 3000,
                                 "orientation": "6"}}
    assert smart.oriented_size(a) == (3000, 4000)
    a["exifInfo"]["orientation"] = "1"
    assert smart.oriented_size(a) == (4000, 3000)


def test_saliency_finds_subject_off_centre():
    # Wide 3:1 photo, flat background, detailed colourful block on the left.
    img = Image.new("RGB", (300, 100), (120, 120, 120))
    d = ImageDraw.Draw(img)
    for i in range(0, 60, 6):
        d.rectangle([10 + i, 20, 13 + i, 80], fill=(220, 30, 30))
    a = {"id": "x", "exifInfo": {"exifImageWidth": 300, "exifImageHeight": 100}}
    plan = smart.plan_asset(a, LANDSCAPE, "blur", lambda _id: img)
    frac = LANDSCAPE / 3.0
    left, right = visible(plan["pos_x"], frac)
    assert plan["focus"] == "saliency"
    assert left <= 0.05 and right >= 0.23


def test_matching_aspect_needs_no_thumbnail():
    a = {"id": "x", "exifInfo": {"exifImageWidth": 1600, "exifImageHeight": 1200}}

    def boom(_id):
        raise AssertionError("thumbnail should not be fetched")

    plan = smart.plan_asset(a, LANDSCAPE, "blur", boom)
    assert plan["focus"] == "none_needed" and plan["pos_x"] == 50.0


def test_orientation_match():
    portrait = {"id": "p", "exifInfo": {"exifImageWidth": 3000, "exifImageHeight": 4000}}
    assert not smart.matches_orientation(portrait, LANDSCAPE)
    assert smart.matches_orientation(portrait, 3 / 4)
