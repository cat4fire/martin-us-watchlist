"""Martin's 2025-02-02 published scans, using native screener fields."""
import datetime as dt
import json
import math
from pathlib import Path
from urllib.request import Request, urlopen
from completed_session import DAILY_FIELDS,freeze_completed

ENDPOINT = 'https://scanner.tradingview.com/america/scan'
FIELDS = ['name','description','exchange','type','close','change','market_cap_basic',
          'Volatility.W','EMA9','EMA21','EMA50','Value.Traded','average_volume_10d_calc',
          'Value.Traded|1W','Perf.1M','premarket_change','sector','premarket_time']
FIELDS += ['time','time[1]']+[key+'[1]' for key in DAILY_FIELDS]

RULES = {
    'Potent': '【完整日固定】市值>1亿美元；周波动>3.5%；收盘>EMA21；完整日成交额>5000万美元。按该日涨幅排序。市值为当前股数回推估算。',
    'Potent盘中': '【动态】市值>1亿美元；周波动>3.5%；当前价格>EMA21；当日累计成交额>5000万美元。开盘后名单会变化。',
    'Premarket': '市值>1亿美元；10日均量>50万股；Vol×Price>5000万美元；按盘前涨幅降序。',
    'Leader': '市值>1亿美元；10日均量>50万股；Vol×Price 1W>3亿美元；按Perf 1M降序。',
}

def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def classify(r):
    values=[r.get(k) for k in ('EMA9','EMA21','EMA50')]
    if not all(finite(v) for v in values): return '均线数据缺失'
    a,b,c=values
    return 'Lead' if a>b>c else 'Lag' if a<b<c else 'Mediocre' if a>b<c or a<b>c else '均线相等'

def evaluate(r, mode):
    """Return pass/fail/unknown, never treat missing fields as a rejected stock."""
    if mode not in RULES: raise ValueError('未知扫描模式')
    if mode=='Potent盘中':mode='Potent'
    checks=[('market_cap_basic',100_000_000)]
    if mode=='Potent':
        checks += [('Volatility.W',3.5),('Value.Traded',50_000_000)]
    elif mode=='Premarket':
        checks += [('average_volume_10d_calc',500_000),('Value.Traded',50_000_000)]
    else:
        checks += [('average_volume_10d_calc',500_000),('Value.Traded|1W',300_000_000)]
    missing=[k for k,_ in checks if not finite(r.get(k))]
    failed=[k for k,v in checks if finite(r.get(k)) and r[k]<=v]
    if mode=='Potent':
        missing += [k for k in ('close','EMA21') if not finite(r.get(k))]
        if not any(k in missing for k in ('close','EMA21')) and r['close']<=r['EMA21']:
            failed.append('close<=EMA21')
    sortkey={'Potent':'change','Premarket':'premarket_change','Leader':'Perf.1M'}[mode]
    # A known failed gate suffices to reject; missing sort data only affects otherwise eligible names.
    if failed: return 'fail', ', '.join(failed)
    if not finite(r.get(sortkey)): missing.append(sortkey)
    if missing: return 'unknown', ', '.join(missing)
    return 'pass',''

def screen(rows, mode, group='全部', small_only=False):
    selected, missing=[],[]
    rejected=0
    for r in rows:
        if mode=='Potent':
            if not r.get('completed'):
                missing.append({'symbol':r['symbol'],'reason':r.get('completed_reason') or '旧快照缺少完整日字段，请重新获取全市场行情'})
                continue
            r=dict(r,**r['completed'])
        else:
            r=dict(r,scan_date='盘前/盘中快照',scan_basis='动态')
        state,reason=evaluate(r,mode)
        if state=='unknown':
            missing.append({'symbol':r['symbol'],'reason':reason})
        elif state=='fail': rejected+=1
        else:
            r=dict(r,group=classify(r))
            if group!='全部' and r['group']!=group: continue
            if small_only and r['market_cap_basic']>=5_000_000_000: continue
            selected.append(r)
    key={'Potent':'change','Potent盘中':'change','Premarket':'premarket_change','Leader':'Perf.1M'}[mode]
    return sorted(selected,key=lambda r:(-r[key],r['symbol'])),missing,rejected

def decode_snapshot(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('data'),list):
        raise ValueError('扫描服务返回了未知格式')
    total=payload.get('totalCount')
    if not isinstance(total,int) or total!=len(payload['data']):
        raise ValueError('服务端返回被截断，拒绝标记为全市场结果')
    rows=[]
    seen=set()
    for item in payload['data']:
        if len(item.get('d',[]))!=len(FIELDS): raise ValueError('字段数量发生变化')
        ticker=item.get('s','')
        if not ticker or ticker in seen: raise ValueError('缺失或重复证券标识')
        seen.add(ticker)
        rows.append(dict(zip(FIELDS,item['d']),ticker=ticker))
    return rows

def fetch_snapshot(cache_dir, opener=urlopen):
    body={'markets':['america'],'symbols':{'query':{'types':[]},'tickers':[]},
          'columns':FIELDS,'range':[0,50000],
          'sort':{'sortBy':'name','sortOrder':'asc'},'options':{'lang':'en'}}
    request=Request(ENDPOINT,data=json.dumps(body).encode(),method='POST',
                    headers={'Content-Type':'application/json','User-Agent':'Mozilla/5.0','Origin':'https://www.tradingview.com'})
    with opener(request,timeout=45) as response:
        payload=json.loads(response.read().decode('utf-8'))
    rows=decode_snapshot(payload)
    fetched=dt.datetime.now(dt.timezone.utc).isoformat()
    path=Path(cache_dir)
    path.mkdir(parents=True,exist_ok=True)
    (path/'screener_snapshot.json').write_text(json.dumps({'fetched_utc':fetched,'fields':FIELDS,'payload':payload},ensure_ascii=False),encoding='utf-8')
    return freeze_completed(rows,cache_dir),fetched

def merge_universe(universe,snapshot):
    """Nasdaq directory controls the scope; retain and report every unmatched issue."""
    by_symbol={}
    for r in snapshot:
        by_symbol.setdefault(r.get('name'),[]).append(r)
    results,missing=[],[]
    exchange={'NASDAQ':{'NASDAQ'},'N':{'NYSE'},'A':{'AMEX'},'P':{'NYSE ARCA','NYSEARCA','AMEX'},'Z':{'CBOE','BATS'},'V':{'IEX'}}
    for security in universe:
        symbol=security['symbol']
        aliases={symbol,security['yahoo'],security['yahoo'].replace('-','.')}
        if '$' in symbol:
            root,series=symbol.split('$',1)
            aliases.add(root+'/P'+series)
        candidates=[r for alias in aliases for r in by_symbol.get(alias,[])]
        unique={r['ticker']:r for r in candidates}
        candidates=list(unique.values())
        expected=exchange.get(security['exchange'])
        if expected:
            candidates=[r for r in candidates if r.get('exchange') in expected]
        if len(candidates)!=1:
            missing.append({'symbol':symbol,'reason':'行情缺失或代码映射不唯一'})
            continue
        results.append(dict(candidates[0],symbol=symbol,security_name=security['name']))
    return results,missing
