"""Export real scanner results as a static, API-free website snapshot."""
import argparse
import datetime as dt
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
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
from incremental_history import update_history

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


def build(report,cache,output,state,today=None,attempts=None,require_all=False):
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
    chart_errors={}
    for symbol,security in securities.items():
        try:
            alias=yahoo_symbol(security)
            raw=json.loads((cache/'history'/f'{alias.encode().hex()}.json').read_text(encoding='utf-8'))
            if raw['symbol']!=alias:continue
            last_date=dt.date.fromisoformat(raw['rows'][-1]['date'])
            validation_day=max(today,last_date+dt.timedelta(days=1))
            if (validation_day-last_date).days>7:validation_day=last_date+dt.timedelta(days=1)
            rows=prepare(raw['rows'],validation_day,display_days=None)
            rows=[r for r in rows if r['date']<str(today)]
            if len(rows)<2:continue
            chart=dict(symbol=symbol,source=raw['source'],adjustment=raw['adjustment'],rows=rows)
            stale_warning='历史缓存：K线截止日期早于最近完整交易日，请核对日期' if rows[-1]['date']<str((attempts or {}).get('required_date','')) else ''
            chart['warning']='；'.join(v for v in (stale_warning,raw.get('quality_warning','')) if v)
            chart['quarantined_bars']=raw.get('quarantined_bars',[])
            chart['quality_warning']=raw.get('quality_warning','')
            relative=f'data/charts/{symbol.encode().hex()}.json'
            write_json(output/relative,chart)
            charts[symbol]=relative
            assessment=assess_setup(dict(raw,rows=rows,warning=stale_warning),today)
            if raw.get('quality_warning'):assessment['setup_reason']=assessment.get('setup_reason','')+'；历史异常日已隔离，详情见图表说明'
            # Expose the same normalized distance used by desktop helper sorting.
            adr=assessment.get('setup_adr') or assessment.get('setup_adr20')
            if adr and assessment.get('setup_distance') is not None:assessment['setup_sort_distance']=assessment['setup_distance']/adr
            assessments[symbol]=assessment
        except (OSError,ValueError,KeyError,TypeError,IndexError) as exc:
            chart_errors[symbol]=str(exc)
    missing_current=sorted({r['symbol'] for preset in modes.values() for r in preset['current'] if r['symbol'] not in charts})
    write_json(state/'chart_errors.json',dict(missing_current=missing_current,details=chart_errors,downloads=(attempts or {}).get('errors',{})))
    if require_all and missing_current:
        raise RuntimeError(f'{len(missing_current)}只当前候选尚无有效日K，本次不替换线上网站；详见chart_errors.json：'+', '.join(missing_current[:20]))
    for mode,preset in modes.items():
        def project(row,historical=False):
            row=dict(row,history_last_seen=archive.get(mode,{}).get(row['symbol'],{}).get('history_last_seen'))
            row['chart']=charts.get(row['symbol'])
            row.update(assessments.get(row['symbol'],dict(setup_status='日K获取失败',setup_reason=(attempts or {}).get('errors',{}).get(row['symbol']) or chart_errors.get(row['symbol'],'缺少有效历史数据'),setup_tier=9)))
            if historical:
                row['setup_reason']='历史入选记录，不代表当前通过筛选；'+(row.get('setup_reason') or '')
            return {key:row.get(key) for key in FIELDS}
        preset['current']=[project(r) for r in preset['current']]
        preset['history']=[project(r,True) for r in preset['history']]
    dates=sorted({r.get('scan_date','') for r in modes['Potent']['current'] if r.get('scan_date')})
    manifest=dict(schema=1,fetched=report['fetched'],built_utc=dt.datetime.now(dt.timezone.utc).isoformat(),complete_session=dates[-1] if dates else None,scope=report['scope'],matched=len(report['rows']),unmatched=len(report['missing']),chart_count=len(charts),modes=modes,download_summary=attempts or {})
    write_json(output/'data/manifest.json',manifest)
    # Keep only public, relevant fields in durable history; no runtime internals or credentials.
    write_json(archive_path,{m:{s:{k:r.get(k) for k in FIELDS if not k.startswith('setup_') and k!='chart'} for s,r in bucket.items()} for m,bucket in archive.items()})
    return manifest


def refresh(cache,output,state,max_charts=0):
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
                write_json(target,dict(symbol=alias,source=seed['source'],adjustment=seed['adjustment'],rows=seed['rows'],requested_history_days=2196,session_date='',fetched_utc='',quality_warning=seed.get('quality_warning',''),quarantined_bars=seed.get('quarantined_bars',[])))
        except (OSError,ValueError,KeyError,TypeError):continue
    universe=[r for r in load_universe(cache) if r['etf']!='Y']
    raw,fetched=fetch_snapshot(cache)
    rows,missing=merge_universe(universe,raw)
    if not rows or len(rows)<len(universe)*.5:raise RuntimeError('行情覆盖异常，保留此前网站，不发布本次结果')
    report=dict(rows=rows,missing=missing,scope=len(universe),fetched=fetched)
    today=history_day()
    # Every current candidate, including the separately evaluated intraday preset.
    groups=[screen(rows,m)[0] for m in RULES]
    ordered=[]
    seen=set()
    for i in range(max(map(len,groups),default=0)):
        for group in groups:
            if i<len(group) and group[i]['symbol'] not in seen:
                seen.add(group[i]['symbol']);ordered.append(group[i])
    required=max((r['completed'].get('scan_date') or '' for r in rows if isinstance(r.get('completed'),dict)),default='') or str(today-dt.timedelta(days=1))
    failures={};results={}
    print(f'All-candidate update: {len(ordered)} unique symbols; required daily bar {required}',flush=True)
    def one(security):
        error=None
        for attempt in range(2):
            try:return update_history(security,cache,today,required)
            except Exception as exc:
                error=exc
                if attempt==0:time.sleep(.5)
        raise RuntimeError(str(error))
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending={pool.submit(one,r):r['symbol'] for r in ordered}
        for future in as_completed(pending):
            symbol=pending[future]
            try:results[symbol]=future.result()
            except Exception as exc:failures[symbol]=str(exc)
            done=len(results)+len(failures)
            if done%25==0 or done==len(ordered):print(f'History progress {done}/{len(ordered)}; available {len(results)}; errors {len(failures)}',flush=True)
    summary=dict(limit='all',attempted=len(ordered),successful=len(results),failed=len(failures),required_date=required,errors=failures,cached=sum(v['status']=='cached' for v in results.values()),incremental=sum(v['status']=='incremental' for v in results.values()),initial=sum(v['status']=='initial' for v in results.values()))
    write_json(state/'download_report.json',summary)
    write_json(cache/'martin_report.json',report)
    result=build(report,cache,output,state,today,summary,require_all=True)
    print(json.dumps(dict(fetched=fetched,charts=result['chart_count'],counts={m:len(v['current']) for m,v in result['modes'].items()},chart_downloads=result['download_summary']),ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--cache',type=Path,default=BASE/'runtime')
    parser.add_argument('--output',type=Path,default=BASE/'dist')
    parser.add_argument('--state',type=Path,default=BASE/'state')
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--max-charts',type=int,default=0,help='Legacy argument accepted; all current candidates are always processed')
    args=parser.parse_args()
    if args.refresh:refresh(args.cache,args.output,args.state,args.max_charts)
    else:
        report=json.loads((args.cache/'martin_report.json').read_text(encoding='utf-8'))
        result=build(report,args.cache,args.output,args.state)
        print(json.dumps(dict(fetched=result['fetched'],charts=result['chart_count'],counts={m:len(v['current']) for m,v in result['modes'].items()}),ensure_ascii=True))
