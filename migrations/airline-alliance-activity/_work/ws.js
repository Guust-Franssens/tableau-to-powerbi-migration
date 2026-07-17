// Resolve a worksheet's encodings/filters to captions using the field registry.
const path = require('path');
const s = require(path.resolve(__dirname, '..', 'migration-spec.json'));
const reg = {};
(s.data_sources || []).forEach(d => (d.fields || []).forEach(f => { reg[f.id] = f; }));
function cap(fid) {
  if (!fid) return null;
  if (fid.startsWith('UNRESOLVED:')) return fid; // keep as-is, flag
  const f = reg[fid];
  if (!f) return 'MISSING(' + fid + ')';
  let extra = f.tableau_formula ? '  ::= ' + String(f.tableau_formula).replace(/\s+/g, ' ').slice(0, 90) : '';
  return `"${f.caption}" [${f.role}/${f.data_type}${f.kind === 'calculated' ? '/calc' : ''}]${extra}`;
}
function enc(e) {
  if (!e) return '(none)';
  if (Array.isArray(e)) return e.map(x => cap(x.field_id) + (x.aggregation ? ' agg=' + x.aggregation : '') + (x.nested_with ? ' NESTED_WITH=' + cap(x.nested_with) : '')).join('\n      ');
  if (e.field_id !== undefined) return cap(e.field_id);
  return JSON.stringify(e);
}
const wsById = {};
s.worksheets.forEach(w => { wsById[w.id] = w; });
const ids = process.argv.slice(2);
for (const id of ids) {
  const w = wsById[id] || s.worksheets.find(x => x.name === id);
  if (!w) { console.log('== NOT FOUND', id); continue; }
  console.log('==============', w.id, '|', w.name, '| mark=', w.mark_type, '==============');
  if (w.title_text) console.log('  title_text:', w.title_text);
  const E = w.encodings || {};
  console.log('  ROWS:    ', enc(E.rows));
  console.log('  COLUMNS: ', enc(E.columns));
  console.log('  COLOR:   ', enc(E.color));
  console.log('  SIZE:    ', enc(E.size));
  console.log('  LABEL:   ', enc(E.label));
  console.log('  DETAIL:  ', enc(E.detail));
  console.log('  TOOLTIP: ', enc(E.tooltip));
  if (E.shape) console.log('  SHAPE:   ', enc(E.shape));
  if (w.reference_lines && w.reference_lines.length) console.log('  REF_LINES:', JSON.stringify(w.reference_lines));
  if (w.filters && w.filters.length) {
    console.log('  FILTERS:');
    w.filters.forEach(f => console.log('     -', cap(f.field_id), '| type=', f.type, '| members=', JSON.stringify(f.members), f.note ? ('| note=' + f.note) : ''));
  }
  if (w.measure_names_values_pivot) console.log('  PIVOT:', JSON.stringify(w.measure_names_values_pivot));
  if (w.manual_sort && w.manual_sort.length) console.log('  MANUAL_SORT:', JSON.stringify(w.manual_sort).slice(0,200));
  console.log('');
}
