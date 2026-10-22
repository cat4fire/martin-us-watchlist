"""Export real scanner results as a static, API-free website snapshot."""
import argparse
import datetime as dt
import json
import math
import time
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'scanner_core'))
from martin_scans import RULES,screen,fetch_snapshot,merge_universe
from market_scan import load_universe
from history_data import load_history,prepare,yahoo_symbol
from entry_priority import assess_setup
from candidate_history import record_candidates

FIELDS=('symbol','ticker','security_name','group','close','change','market_cap_basic','Volatility.W','Value.Traded','average_volume_10d_calc','Value.Traded|1W','Perf.1M','premarket_change','scan_date','sector','history_last_seen','setup_status','setup_distance','setup_trigger','setup_invalidation','setup_risk','setup_asof','setup_reason','setup_tier','setup_sort_distance','chart')


def clean(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
    if isinstance(value,list):return [clean(v) for v in value]
    return value


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(clean(value),ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
    temp.replace(path)


def history_day(now=None):
    now=now or dt.datetime.now(ZoneInfo('America/New_York'))
    # Existing history loader excludes its `today`; after close, allow today's final bar.
    return now.date()+dt.timedelta(days=1) if (now.hour,now.minute)>=(16,15) else now.date()


def build(report,cache,output,state,today=None,attempts=None):
    today=today or history_day()
    archive_path=state/'candidate_history.json'
    archive=record_candidates(archive_path,report['rows'],report['fetched'])
    modes={}
    securities={}
    for mode,rule in RULES.items():
        rows,missing,rejected=screen(report['rows'],mode)
        for row in rows:securities[row['symbol']]=row
        for row in archive.get(mode,{}).values():securities.setdefault(row['symbol'],row)
        modes[mode]=dict(rule=rule,current=rows,history=list(archive.get(mode,{}).values()),missing=len(missing),rejected=rejected)
    charts={}
    assessments={}
    for symbol,security in securities.items():
        try:
            alias=yahoo_symbol(security)
            raw=json.loads((cache/'history'/f'{alias.encode().hex()}.json').read_text(encoding='utf-8'))
            if raw['symbol']!=alias:continue
            rows=prepare(raw['rows'],today,display_days=None)
            if len(rows)<2:continue
            chart=dict(symbol=symbol,source=raw['source'],adjustment=raw['adjustment'],rows=rows)
            chart['warning']='K线滞后于选股日期，请核对截止日' if security.get('scan_date','')[:10].count('-')==2 and rows[-1]['date']<security['scan_date'][:10] else ''
            relative=f'data/charts/{symbol.encode().hex()}.json'
            write_json(output/relative,chart)
            charts[symbol]=relative
            assessment=assess_setup(dict(raw,rows=rows,warning=chart['warning']),today)
            # Expose the same normalized distance used by desktop helper sorting.
            adr=assessment.get('setup_adr') or assessment.get('setup_adr20')
            if adr and assessment.get('setup_distance') is not None:assessment['setup_sort_distance']=assessment['setup_distance']/adr
            assessments[symbol]=assessment
        except (OSError,ValueError,KeyError,TypeError):continue
    for mode,preset in modes.items():
        def project(row,historical=False):
            row=dict(row,history_last_seen=archive.get(mode,{}).get(row['symbol'],{}).get('history_last_seen'))
            row['chart']=charts.get(row['symbol'])
            if historical:
                row.update(setup_status='历史快照·需复核',setup_reason='历史入选价格和日期，非当前买点')
                for key in ('setup_distance','setup_trigger','setup_invalidation','setup_risk','setup_asof'):row[key]=None
            else:
                row.update(assessments.get(row['symbol'],dict(setup_status='未发布日K',setup_reason='本次尚无可用历史K线',setup_tier=9)))
            return {key:row.get(key) for key in FIELDS}
        preset['current']=[project(r) for r in preset['current']]
        preset['history']=[project(r,True) for r in preset['history']]
    dates=sorted({r.get('scan_date','') for r in modes['Potent']['current'] if r.get('scan_date')})
    manifest=dict(schema=1,fetched=report['fetched'],built_utc=dt.datetime.now(dt.timezone.utc).isoformat(),complete_session=dates[-1] if dates else None,scope=report['scope'],matched=len(report['rows']),unmatched=len(report['missing']),chart_count=len(charts),modes=modes,download_summary=attempts or {})
    write_json(output/'data/manifest.json',manifest)
    # Keep only public, relevant fields in durable history; no runtime internals or credentials.
    write_json(archive_path,{m:{s:{k:r.get(k) for k in FIELDS if not k.startswith('setup_') and k!='chart'} for s,r in bucket.items()} for m,bucket in archive.items()})
    return manifest


def select_chart_batch(ordered,cached_symbols,cursor,limit):
    if not ordered:return []
    offset=cursor%len(ordered)
    rotating=ordered[offset:]+ordered[:offset]
    priority=[r for r in rotating if r['symbol'] in cached_symbols]
    batch=[]
    seen=set()
    for row in priority[:limit//2]+rotating:
        if row['symbol'] not in seen:
            seen.add(row['symbol'])
            batch.append(row)
        if len(batch)>=limit:break
    return batch


def refresh(cache,output,state,max_charts=500):
    # Reuse the shipped real chart files if a new runner has no Actions cache yet.
    def seeds():
        for path in (output/'data/charts').glob('*.json'):
            try:yield json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue
        for path in (output/'data/chart-packs').glob('*.json'):
            try:yield from json.loads(path.read_text(encoding='utf-8'))['charts'].values()
            except (OSError,ValueError,KeyError,TypeError):continue
    for seed in seeds():
        try:
            alias=yahoo_symbol(seed)
            target=cache/'history'/f'{alias.encode().hex()}.json'
            if not target.exists():
                write_json(target,dict(symbol=alias,source=seed['source'],adjustment=seed['adjustment'],rows=seed['rows'],requested_history_days=2196,session_date='',fetched_utc=''))
        except (OSError,ValueError,KeyError,TypeError):continue
    universe=[r for r in load_universe(cache) if r['etf']!='Y']
    raw,fetched=fetch_snapshot(cache)
    rows,missing=merge_universe(universe,raw)
    if not rows or len(rows)<len(universe)*.5:raise RuntimeError('行情覆盖异常，保留此前网站，不发布本次结果')
    report=dict(rows=rows,missing=missing,scope=len(universe),fetched=fetched)
    today=history_day()
    # Prioritize Potent, then leading/premarket names; round-robin full candidate coverage across days.
    groups=[screen(rows,m)[0] for m in ('Potent','Leader','Premarket')]
    ordered=[]
    seen=set()
    for i in range(max(map(len,groups),default=0)):
        for group in groups:
            if i<len(group) and group[i]['symbol'] not in seen:
                seen.add(group[i]['symbol']);ordered.append(group[i])
    cursor_path=state/'chart_cursor.json'
    cursor=json.loads(cursor_path.read_text(encoding='utf-8')).get('cursor',0) if cursor_path.exists() else 0
    # Refresh published candidates before adding missing charts, within a bounded free-service budget.
    cached_symbols={r['symbol'] for r in ordered if (cache/'history'/f'{r["symbol"].replace(".","-").encode().hex()}.json').exists()}
    batch=select_chart_batch(ordered,cached_symbols,cursor,max_charts)
    failures=[];success=0
    attempted=0
    deadline=time.monotonic()+20*60
    for security in batch:
        if time.monotonic()>=deadline:break
        attempted+=1
        try:
            payload=load_history(security,cache,today=today)
            if payload.get('warning'):failures.append(security['symbol'])
            else:success+=1
        except Exception:failures.append(security['symbol'])
    write_json(cursor_path,dict(cursor=cursor+max(1,attempted//2)))
    write_json(cache/'martin_report.json',report)
    result=build(report,cache,output,state,today,dict(limit=max_charts,attempted=attempted,successful=success,failed=len(failures),time_budget_reached=attempted<len(batch)))
    print(json.dumps(dict(fetched=fetched,charts=result['chart_count'],counts={m:len(v['current']) for m,v in result['modes'].items()},chart_downloads=result['download_summary']),ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--cache',type=Path,default=BASE/'runtime')
    parser.add_argument('--output',type=Path,default=BASE/'dist')
    parser.add_argument('--state',type=Path,default=BASE/'state')
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--max-charts',type=int,default=500)
    args=parser.parse_args()
    if not 1<=args.max_charts<=5000:parser.error('--max-charts must be between 1 and 5000')
    if args.refresh:refresh(args.cache,args.output,args.state,args.max_charts)
    else:
        report=json.loads((args.cache/'martin_report.json').read_text(encoding='utf-8'))
        result=build(report,args.cache,args.output,args.state)
        print(json.dumps(dict(fetched=result['fetched'],charts=result['chart_count'],counts={m:len(v['current']) for m,v in result['modes'].items()}),ensure_ascii=True))
