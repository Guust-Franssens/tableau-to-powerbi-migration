// Print full DAX body for named measures from Flight Activity.tmdl
const fs = require('fs');
const path = require('path');
const p = path.resolve(__dirname, '..', 'fabric', 'AirlineAllianceActivity.SemanticModel', 'definition', 'tables', 'Flight Activity.tmdl');
const lines = fs.readFileSync(p, 'utf8').split(/\r?\n/);
const want = process.argv.slice(2);
function norm(s){ return s.replace(/'/g,'').trim(); }
let i = 0;
const blocks = [];
while (i < lines.length) {
  const m = lines[i].match(/^\s*measure\s+('([^']+)'|[^=]+?)\s*=(.*)$/);
  if (m) {
    const name = norm(m[2] || m[1]);
    const start = i;
    const indent = lines[i].match(/^\s*/)[0].length;
    let body = [lines[i]];
    i++;
    while (i < lines.length) {
      if (/^\s*measure\s/.test(lines[i])) break;
      if (/^\s*column\s/.test(lines[i])) break;
      if (/^\s*partition\s/.test(lines[i])) break;
      // stop if we hit a line with indent <= measure indent that's a new property at table level
      body.push(lines[i]);
      i++;
    }
    blocks.push({ name, body: body.join('\n') });
  } else i++;
}
for (const w of want) {
  const b = blocks.find(x => x.name === norm(w));
  console.log('=====', w, '=====');
  console.log(b ? b.body.replace(/\n\s*\n\s*\n/g,'\n') : '(NOT FOUND)');
  console.log('');
}
