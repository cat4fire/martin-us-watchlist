"""Fail a release if a static snapshot is broken or points outside its data directory."""
import json
from pathlib import Path

root=Path(__file__).resolve().parent/'dist'
manifest=json.loads((root/'data/manifest.json').read_text(encoding='utf-8'))
assert manifest['matched']<=manifest['scope']
assert manifest['fetched']
loaded={}
for name,preset in manifest['modes'].items():
    assert len({r['symbol'] for r in preset['current']})==len(preset['current'])
    for row in preset['current']+preset['history']:
        chart=row.get('chart')
        if not chart:continue
        path=(root/chart).resolve()
        assert path.is_relative_to((root/'data/charts').resolve()) or path.is_relative_to((root/'data/chart-packs').resolve())
        if chart not in loaded:loaded[chart]=json.loads(path.read_text(encoding='utf-8'))
        data=loaded[chart]
        if 'charts' in data:data=data['charts'][row['symbol']]
        assert data['symbol']==row['symbol']
        assert all(a['date']<b['date'] for a,b in zip(data['rows'],data['rows'][1:]))
        assert not any(r.get('display_only') or r.get('is_partial') for r in data['rows'])
print(f'PASS: {len(manifest["modes"])} presets, {manifest["chart_count"]} chart files, unique rows, full-day history and safe paths')
