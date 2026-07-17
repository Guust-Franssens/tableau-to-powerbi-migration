// Inspect data_sources field registry + build field_id -> caption/formula map
const path = require('path');
const s = require(path.resolve(__dirname, '..', 'migration-spec.json'));
const ds = s.data_sources || [];
console.log('data_sources count:', ds.length);
ds.forEach(d => {
  console.log('--- DS', d.id, '| keys:', Object.keys(d).join(','));
  if (d.fields) console.log('   fields:', d.fields.length);
});
// dump shape of first few fields
const d0 = ds[0];
if (d0 && d0.fields) {
  console.log('=== sample field keys ===', Object.keys(d0.fields[0]).join(','));
  d0.fields.slice(0, 6).forEach(f => console.log(JSON.stringify(f).slice(0, 300)));
}
