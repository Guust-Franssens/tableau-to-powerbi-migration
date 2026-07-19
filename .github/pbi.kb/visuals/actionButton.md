# actionButton (static Web URL button)

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

🟡 template-ready — the `visualContainerObjects.visualLink` WebUrl encoding **validates clean**
(`powerbi-report-author validate`, 0 errors / 0 warnings) and is used for all 6 hardcoded links +
social chips in `migrations\interactive-resume`. Desktop click-through was **not** render-captured
(the shared Desktop Bridge was held by sibling parallel builds), so it is not yet 🟢. Promote to 🟢
after a Desktop capture confirms the button navigates.

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
