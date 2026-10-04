// picture_immich_smart, full-bleed Immich photo framed around its subject.
//
// server.py does the framing: it returns `fit` ("cover" or "contain") and
// object-position percentages that keep faces (or the busiest region) in
// frame. "contain" means the subject wouldn't fit a crop, so the photo is
// letterboxed over a blurred, enlarged copy of itself instead of bars.

const ENTITIES = new Map([
  ["&", "&amp;"], ["<", "&lt;"], [">", "&gt;"], ['"', "&quot;"], ["'", "&#39;"],
]);

// Text and attribute values go through this before landing in innerHTML.
const esc = (value) =>
  Array.from(value == null ? "" : String(value), (ch) => ENTITIES.get(ch) ?? ch).join("");

// "12 Mar 2024 · Seattle", from whichever of the two the photo has.
function caption({ taken_at: takenAt, city }) {
  const when = takenAt ? new Date(takenAt) : null;
  const date = when && !Number.isNaN(when.valueOf())
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(when)
    : "";
  return [date, city].filter(Boolean).join(" · ");
}

const STYLES = `
  <link rel="stylesheet" href="/static/style/spectra-widgets.css">
  <style>
    .pis { position: relative; width: 100%; height: 100%; overflow: hidden;
           container-type: size; background: var(--surface); }
    .pis img { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
    .pis .bg { object-fit: cover; filter: blur(4cqmin) saturate(1.2); transform: scale(1.15); }
    .pis-note { position: absolute; inset: 0; display: flex; flex-direction: column;
                align-items: center; justify-content: center; gap: 3cqmin; padding: 8cqmin;
                text-align: center; color: var(--text-secondary); font-size: 4cqmin; }
    .pis-note .ph { font-size: 12cqmin; color: var(--text-primary); }
    .pis-caption { position: absolute; left: 0; right: 0; bottom: 0;
                   padding: 1.2cqmin 2.4cqmin; font-size: 2.4cqmin; line-height: 1.25;
                   letter-spacing: 0.04em; color: var(--surface); background: var(--text-primary); }
  </style>`;

function note(icon, text) {
  return `<div class="pis-note"><i class="ph ph-bold ${icon}"></i><p>${esc(text)}</p></div>`;
}

export default function render(shadow, ctx) {
  const data = (ctx && ctx.data) || {};
  let body;
  if (data.error) {
    body = note("ph-warning-circle", data.error);
  } else if (!data.image_url) {
    body = note("ph-image-broken", "No photo to show yet.");
  } else {
    const src = esc(data.image_url);
    const letterbox = data.fit === "contain";
    const x = Number(data.pos_x ?? 50);
    const y = Number(data.pos_y ?? 50);
    const text = data.show_caption ? caption(data) : "";
    // Inline styles so the computed crop wins over any stylesheet default.
    body = [
      letterbox ? `<img class="bg" src="${src}" alt="">` : "",
      `<img src="${src}" alt="" style="object-fit:${letterbox ? "contain" : "cover"};`
        + `object-position:${x}% ${y}%;">`,
      text ? `<div class="pis-caption">${esc(text)}</div>` : "",
    ].join("");
  }
  shadow.innerHTML = `${STYLES}<div class="pis" data-widget="picture_immich_smart"`
    + ` data-focus="${esc(data.focus)}">${body}</div>`;
}
