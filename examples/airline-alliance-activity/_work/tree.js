// Dump a dashboard zone tree as an indented outline.
// Usage: node tree.js "Alliance Page"
const path = require('path');
const specPath = path.resolve(__dirname, '..', 'migration-spec.json');
const s = require(specPath);
const name = process.argv[2] || 'Alliance Page';
const d = s.dashboards.find(x => x.name === name);
if (!d) { console.error('no dashboard', name); process.exit(1); }
console.log('DASH', d.name, 'size=', JSON.stringify(d.size));

function txt(z) {
  const parts = [];
  if (z.worksheet_id) parts.push('WS=' + z.worksheet_id);
  if (z.field_id) parts.push('FLD=' + z.field_id);
  if (z.text_html) parts.push('TXT="' + z.text_html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim().slice(0, 50) + '"');
  if (z.background_color) parts.push('BG=' + z.background_color);
  return parts.join(' ');
}
function walk(z, depth) {
  const pad = '  '.repeat(depth);
  const pos = '[x=' + z.x + ' y=' + z.y + ' w=' + z.w + ' h=' + z.h + ']';
  console.log(pad + z.type + ' id=' + z.id + ' ' + pos + ' ' + txt(z));
  (z.children || []).forEach(c => walk(c, depth + 1));
}
walk(d.zones, 0);
