# actionButton (static Web URL button)

> ## 🔴 DO NOT USE for a *visible* button in Power BI Desktop (render-verified 2.156)
> `actionButton` **silently ignores its entire `visual.objects` block on render** (fill / text /
> tileShape) and draws as a **blank rectangle**, while `powerbi-report-author validate` still reports
> 0-errors and the Format → Action pane still shows the URL. This is exactly the trap the 🟡 verdict
> below warned might exist — it is now **confirmed real** (all 12 buttons in
> `migrations\interactive-resume` rendered blank until converted). **Use the `shape` visual instead**
> (`visuals/shape.md`, 🟢): it supports the *identical* container-level `visualLink` WebUrl action AND
> actually renders `fill`/`outline`/`tileShape`. Keep this file only as the reference for the
> `visualLink` encoding (which `shape` reuses verbatim) and as a cautionary example of a
> validate-clean-but-render-broken visual.

Use for a Tableau **URL / hyperlink dashboard action** whose target is a fixed/hardcoded URL
(social icons, "click here" links, external references) — i.e. a link that is **not** carried in a
`dataCategory: WebUrl` model column. When the URL *is* a model column, prefer binding that column
natively (a table/visual column typed as Web URL) instead of a button.

## Roles

`actionButton` has **no data field wells** — it is a shape/navigation visual, not a data visual. Its
behavior is entirely formatting + a container-level link action.

## Key formatting objects

- `visual.objects.shape[].properties.tileShape` — button silhouette (`'oval'`, `'rectangle'`,
  `'rectangleRounded'`, ...). Ovals make good round social/link chips.
- `visual.objects.fill[]` — `show`, `fillColor.solid.color`, `transparency` (`"0D"` = opaque).
  Requires `selector.id = "default"`. Set `show:false` for an invisible click hotspot over other art.
- `visual.objects.outline[]` — border; `selector.id = "default"`.
- `visual.objects.text[]` — embedded label: `show`, `text`, `fontFamily`, `fontSize` (`"NND"`),
  `bold`, `fontColor.solid.color`, `horizontalAlignment` (`'center'`). `selector.id = "default"`.
- **The Web URL action itself is NOT a button formatting object.** actionButton exposes no `link`
  object under `visual.objects`. The action lives at the **container** level:
  `visual.visualContainerObjects.visualLink[0].properties`:
  - `show` = Literal `"true"`
  - `type` = Literal `"'WebUrl'"` (string literal — single-quoted **inside** the value)
  - `webUrl` = Literal `"'https://...'"` (single-quoted URL)
  - Other `visualLink` types (all Literal, from the downloaded visualContainer schema): `'Bookmark'`
    (+ `bookmark`), `'Back'`, `'PageNavigation'` (+ `navigationSection`), `'DrillThrough'`
    (+ `drillthroughSection`), `'Qna'`, `'WebUrl'` (+ `webUrl`).
- Container `background`/`border` `show:false` and `padding` all `"0D"` for a clean chip (0D padding
  also avoids `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR`-class floors on tiny buttons).

## Tableau idiom

Tableau `type="url"` dashboard actions with a hardcoded/placeholder target → one `actionButton` per
link. For an **invisible clickable hotspot** over existing artwork (e.g. a link over a chart mark or
image), set `fill.show:false`, `outline.show:false`, and omit `text` — only the `visualLink` remains.

## Tier verdict

🔴 **render-broken for visible buttons** — the `visualContainerObjects.visualLink` WebUrl encoding
validates clean (`powerbi-report-author validate`, 0 errors / 0 warnings) but Desktop 2.156 **does not
render `visual.objects`** (fill/text/tileShape), so the button appears as a blank rectangle. This was
observed live in `migrations\interactive-resume` (all 12 buttons blank) and fixed by converting each
to `shape` (see `visuals/shape.md`, 🟢). The `visualLink` block itself is correct and is reused
verbatim by `shape`. Do **not** promote this to 🟢; use `shape` for anything that must be seen.

## Human Desktop capture instructions

Open the report in Power BI Desktop, select the button, confirm the **Format → Action** pane shows
Type = *Web URL* with the expected URL, then Ctrl+click the button and confirm it opens the URL. Save
and copy the resulting `visual.json` back here as ground truth before marking 🟢.

## Binding notes

No model binding. The exemplar `actionButton.visual.json` is a round social chip (`tileShape:'oval'`,
teal fill, white "in" label) linking to `https://example.com/`. Replace `text` and the `visualLink`
`webUrl` literal per link. `name` must be globally unique across the report; if you also add
visual-level filters elsewhere, keep every `filterConfig` filter `name` globally unique too
(`PBIR_FILTER_NAME_DUPLICATE_GLOBAL`).
