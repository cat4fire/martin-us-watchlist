"""Local record of candidates, kept separately for each scan preset."""
import json
from martin_scans import RULES, screen


def record_candidates(path, rows, fetched):
    if path.exists():
        archive=json.loads(path.read_text(encoding='utf-8'))
    else:
        archive={}
    for mode in RULES:
        bucket=archive.setdefault(mode,{})
        candidates,_,_=screen(rows,mode,'全部',False)
        for row in candidates:
            old=bucket.get(row['symbol'],{})
            if fetched>=old.get('history_last_seen',''):
                bucket[row['symbol']]=dict(row,history_last_seen=fetched)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(archive,ensure_ascii=False),encoding='utf-8')
    temporary.replace(path)
    return archive


def removed_candidates(archive,mode,current,group='全部',small=False):
    symbols={r['symbol'] for r in current}
    return [dict(r) for symbol,r in archive.get(mode,{}).items()
            if symbol not in symbols and (group=='全部' or r.get('group')==group)
            and (not small or isinstance(r.get('market_cap_basic'),(int,float)) and r['market_cap_basic']<5e9)]
