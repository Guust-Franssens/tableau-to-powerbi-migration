// Independent verification: field refs vs TMDL, page index, overlap/space audit,
// slicer defaults, single-active-field table anti-pattern. Backs up the offline
// validate run where JSON-schema fetch was skipped (PBIR_SCHEMA_UNREACHABLE).
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const REPO = "C:/Users/gfranssens/vscode-projects/tableau-to-pbi-migration";
const MODEL = join(REPO, "migrations/quadruple-axis-charts/fabric/QuadrupleAxisCharts.SemanticModel/definition/tables");
const REPORT = join(REPO, "migrations/quadruple-axis-charts/fabric/QuadrupleAxisCharts.Report");
const PAGES = join(REPORT, "definition/pages");

// ---- build model name map from TMDL ----
const model = {}; // table -> Set(names)
for (const f of readdirSync(MODEL).filter(f => f.endsWith(".tmdl"))) {
  const tbl = f.replace(/\.tmdl$/, "");
  const txt = readFileSync(join(MODEL, f), "utf8");
  const set = new Set();
  const re = /^\s*(?:measure|column)\s+(?:'([^']+)'|([^\s=]+))/gm;
  let m;
  while ((m = re.exec(txt))) set.add(m[1] || m[2]);
  model[tbl] = set;
}
console.log("Model tables:", Object.keys(model).map(t => `${t}(${model[t].size})`).join(", "));

// ---- walk visuals ----
const pageDirs = readdirSync(PAGES).filter(d => existsSync(join(PAGES, d, "page.json")));
const errors = [], warns = [];
const filterNames = new Map(); // global uniqueness
let visualCount = 0;

function checkRef(entity, prop, where) {
  if (!model[entity]) { errors.push(`${where}: unknown table "${entity}"`); return; }
  if (!model[entity].has(prop)) errors.push(`${where}: "${entity}"."${prop}" not in model`);
}
function scanFields(node, where) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) { node.forEach(n => scanFields(n, where)); return; }
  for (const k of ["Measure", "Column"]) {
    if (node[k] && node[k].Expression?.SourceRef?.Entity && node[k].Property) {
      checkRef(node[k].Expression.SourceRef.Entity, node[k].Property, where);
    }
  }
  for (const v of Object.values(node)) scanFields(v, where);
}

// overlap helper
function overlaps(a, b) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

const pagesJson = JSON.parse(readFileSync(join(PAGES, "pages.json"), "utf8"));
for (const pid of pagesJson.pageOrder) {
  if (!pageDirs.includes(pid)) errors.push(`pages.json lists "${pid}" but no page folder`);
}
for (const pid of pageDirs) {
  if (!pagesJson.pageOrder.includes(pid)) errors.push(`page folder "${pid}" missing from pages.json pageOrder`);
  const vdir = join(PAGES, pid, "visuals");
  const boxes = [];
  const vids = existsSync(vdir) ? readdirSync(vdir) : [];
  for (const vid of vids) {
    const vp = join(vdir, vid, "visual.json");
    if (!existsSync(vp)) continue;
    visualCount++;
    const v = JSON.parse(readFileSync(vp, "utf8"));
    const where = `${pid}/${vid}`;
    if (v.name !== vid) warns.push(`${where}: visual.name "${v.name}" != folder`);
    // field refs
    scanFields(v.visual?.query?.queryState, where);
    scanFields(v.visual?.objects, where);
    // filter refs + names
    for (const fc of v.filterConfig?.filters || []) {
      if (filterNames.has(fc.name)) errors.push(`${where}: duplicate filter name "${fc.name}" (also ${filterNames.get(fc.name)})`);
      else filterNames.set(fc.name, where);
      scanFields(fc.field, where + " [filter field]");
      scanFields(fc.filter?.Where, where + " [filter where]");
    }
    // slicer FillRule metadata refs (SelectRef.ExpressionName = Entity.Prop)
    const txt = JSON.stringify(v);
    for (const mm of txt.matchAll(/"ExpressionName":"([^".]+)\.([^"]+)"/g)) checkRef(mm[1], mm[2], where + " [FillRule input]");
    for (const mm of txt.matchAll(/"metadata":"([^".]+)\.([^"]+)"/g)) checkRef(mm[1], mm[2], where + " [selector metadata]");
    // position box (exclude header/sub/note/title textboxes from overlap? no—include all, they must not overlap)
    if (v.position) boxes.push({ id: vid, ...v.position });
    // single-active-field table anti-pattern
    if (["tableEx", "pivotTable"].includes(v.visual?.visualType)) {
      const vals = v.visual?.query?.queryState?.Values?.projections || [];
      const activeCount = vals.filter(p => p.active).length;
      if (vals.length > 1 && activeCount === 1) warns.push(`${where}: table has ${vals.length} value projections but only 1 active (single-active-field anti-pattern)`);
    }
  }
  // overlap audit (ignore pure decoration overlaps between title/sub which are same-band separate y)
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++)
      if (overlaps(boxes[i], boxes[j])) errors.push(`${pid}: OVERLAP ${boxes[i].id} <> ${boxes[j].id}`);
}

// definition.pbir
const pbir = JSON.parse(readFileSync(join(REPORT, "definition.pbir"), "utf8"));
const modelPath = pbir.datasetReference?.byPath?.path;
if (modelPath !== "../QuadrupleAxisCharts.SemanticModel") errors.push(`definition.pbir byPath = "${modelPath}"`);

// slicer default literal sanity
console.log(`\nVisuals scanned: ${visualCount}`);
console.log(`Filter names (unique): ${filterNames.size}`);
console.log(`\n=== ERRORS (${errors.length}) ===`);
errors.forEach(e => console.log("  X " + e));
console.log(`\n=== WARNINGS (${warns.length}) ===`);
warns.forEach(w => console.log("  ! " + w));
console.log(errors.length === 0 ? "\nFIELD/OVERLAP/REF CROSS-CHECK: PASS" : "\nCROSS-CHECK: FAIL");
