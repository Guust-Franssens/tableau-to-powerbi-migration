# shape (filled silhouette + Web URL link) — 🟢 render-verified

Use for a Tableau **image/shape/badge composition mark** or a **hardcoded URL dashboard action**
(social chips, hexagon badges, click hotspots over other art). This is the **preferred encoding for a
static Web-URL button** — see the critical `actionButton` warning below.

## Why `shape`, not `actionButton` (render-verified in Desktop 2.156)

`actionButton` **silently ignores its entire `visual.objects` block on render** (fill / text /
tileShape) and draws as a **blank rectangle**, even though `powerbi-report-author validate` passes
0-errors and the Format → Action pane shows the URL. The `shape` visual type supports the **same**
container-level `visualContainerObjects.visualLink[0]` WebUrl action **and** actually renders its
`fill` / `outline` / `tileShape` — so it is clickable *and* visible. Convert every static-URL
`actionButton` to `shape`, keeping the identical `visualLink`.

Verified: all 12 hardcoded links in `migrations\interactive-resume` (6 hexagon badges, 3 social
chips, ~3 hobby hotspots) render as filled shapes **and** navigate on Ctrl+click. Desktop
2.156.879.0, PID-pinned screenshot capture.

## Roles

No data field wells — `shape` is a static/navigation visual. Behavior is formatting + a
container-level link.

## Key formatting objects (`visual.objects`)

- `shape[].properties.tileShape` — silhouette Literal: `'hexagon'`, `'oval'`, `'rectangle'`,
  `'rectangleRounded'`, `'triangle'`, ... Hexagon/oval both render correctly.
  **`shape[]` takes NO `selector`** (single-instance object).
- `fill[].properties` — `show`, `fillColor.solid.color` (Literal `'#RRGGBB'`), `transparency`
  (`"0D"` opaque … `"100D"` fully transparent). **Requires `selector.id = "default"`.**
- `outline[].properties.show` — border on/off. **Requires `selector.id = "default"`.**
  NOTE: even with `outline.show:false` **and** `fill.transparency:100D`, Desktop still draws a very
  faint default border. A "fully invisible" hotspot is therefore not perfectly invisible — acceptable
  for a click target, but don't rely on it being truly hidden over busy art.

## ⚠️ Embedded `text` object does NOT render (Desktop 2.156)

`shape` **accepts** a `text` object (`show`/`text`/`fontFamily`/`fontSize`/`bold`/`fontColor`/
`horizontalAlignment`/`verticalAlignment`) and it **validates clean**, but the text **does not paint
on render** — the shape shows fill only, no label. Confirmed empirically **with and without** a
`selector` on the text object (the `text` object is single-instance: `formatting list-objects shape`
lists it *without* the "(selector: default)" annotation, unlike fill/outline/shape). Removing/adding
the selector does not change the outcome.

The exemplar `shape.visual.json` here still carries a `text:"DTC"` object (harmless no-op) to document
exactly this: the hexagon fill + WebUrl link render, the embedded "DTC" does not.

### Workaround — overlay a separate `textbox` visual

`textbox` visuals render text 100% reliably. To label a shape, place a **separate centered `textbox`
on top** at a higher `z` (e.g. `z=9500` over a `z=9000` shape), sized/positioned to the shape center
(`y ≈ shape_cy + shape_h/2 − label_h/2` for vertical centering; paragraph `horizontalAlignment:
center`). **Only overlay non-clickable shapes** — a textbox over a clickable shape intercepts the
click and kills the `visualLink`. For clickable badges/chips, put the label *adjacent* (beside) the
shape instead, not on top. (In interactive-resume: the 4 EXPERIENCE circles get centered textbox
overlays `circ-1-lbl`..`circ-4-lbl`; the clickable hexagon badges use adjacent `bl-*` labels beside
them.)

## Web URL action (container level, identical to actionButton)

`visual.visualContainerObjects.visualLink[0].properties`:
- `show` = Literal `"true"`
- `type` = Literal `"'WebUrl'"` (string literal, single-quoted **inside** the value)
- `webUrl` = Literal `"'https://...'"`

Set container `background.show:false`, `border.show:false`, and all `padding` `"0D"` for a clean chip
(0D padding also avoids `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR`-class floors on tiny shapes). Other
`visualLink` `type`s (all Literal): `'Bookmark'` (+`bookmark`), `'Back'`, `'PageNavigation'`
(+`navigationSection`), `'DrillThrough'` (+`drillthroughSection`), `'Qna'`, `'WebUrl'` (+`webUrl`).

## Invisible click hotspot over artwork

`tileShape:'rectangleRounded'`, `fill.show:true` + `transparency:"100D"` (keeps the clickable bounds
alive while nearly invisible), `outline.show:false`, no `text`, only the `visualLink` carries the URL.
Overlay it at the position of the underlying mark (e.g. over a chart bar / image) so the mark carries
the data while the hotspot carries the link. Mind the faint-border caveat above.

## Tier verdict

🟢 render-verified — shape fill + tileShape (hexagon/oval/rectangleRounded) + `visualLink` WebUrl all
render and click in Desktop 2.156. The one hard gotcha (embedded `text` doesn't render → use a textbox
overlay) is baked into this note. Exemplar: `shape.visual.json` (a pink hexagon badge from
`migrations\interactive-resume`, `bg-dtc`).
