# Picture, Immich (Smart Crop)

A [Tesserae](https://tesserae.ink) widget that shows full-bleed photos from
[Immich](https://immich.app) and **crops each one to fit the frame around its
subject**, so you never have to hand-crop photos for a landscape or portrait
display.

## How it frames a photo

1. **Faces first.** Immich's face detection already stores a box around every
   face. The widget places the crop so all faces fit, with headroom above them,
   and puts them on the upper third of the frame. Tiny background faces are
   ignored.
2. **Busiest region otherwise.** With no faces (landscapes, pets, food), a
   small thumbnail is scored for edge detail and colour, and the crop slides to
   the most interesting part instead of the centre.
3. **When the faces can't fit** (say, a tall group shot on a wide frame), the
   **"When faces don't fit"** option decides:
   - *Letterbox on blurred copy* (default): the whole photo, over a blurred,
     enlarged copy of itself instead of black bars.
   - *Skip to another photo*: picks a different photo from the batch.
   - *Crop around the largest face*: crops anyway, keeping the main face.

The crop is applied with CSS `object-position`, so photos are never re-encoded
or modified.

## Requirements

- Tesserae with the **Picture, Immich** widget installed and an Immich library
  connected under **Widgets → Immich**. This widget reuses those connections
  and that widget's image proxy, so the API key is entered only once and never
  reaches the display.
- Immich v2 or newer (uses `POST /api/search/random` with `withPeople`).
  Face-aware framing needs Immich's face detection enabled (on by default).

## Options

| Option | Values |
|---|---|
| Library | An Immich library from Widgets → Immich |
| Mode | Random from album (default), Random from library |
| Album | For "Random from album" |
| When faces don't fit the crop | Letterbox on blurred copy (default), Skip, Crop around largest face |
| Orientation | Any photo (default), Prefer photos matching the frame |
| Show date / place caption | Off by default |

## Install

Copy the `picture_immich_smart/` folder into your Tesserae `data/plugins/`
directory and restart Tesserae (or register it from Tesserae Studio). Then
pick **Picture, Immich (Smart Crop)** for a cell, ideally a one-cell
dashboard for a full-screen photo frame.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pillow numpy pytest
.venv/bin/python -m pytest tests        # framing logic, no network
scripts/sync-to-studio.sh               # copy into a Tesserae Studio workspace
```

`tests/test_framing.py` covers the pure framing functions in
`picture_immich_smart/server.py` (crop axis, face placement and headroom,
the fallbacks, saliency, EXIF orientation). With Studio, run its
`lint_widget`, `register_widget` and `faithful_render` tools after syncing.

## License

[MIT](LICENSE). It uses the Picture, Immich widget's saved connections and
image proxy at runtime but contains none of its code.
