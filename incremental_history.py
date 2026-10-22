"""Incremental website history: same-source overlap check, durable cache, no synthetic bars."""
import datetime as dt
import json
import math
from pathlib import Path
from urllib.parse import urlencode,quote
from urllib.request import Request,urlopen
from history_data import HISTORY_DAYS,load_history,prepare,validate,yahoo_symbol,parse_nasdaq,_download_lock


def fetch_range(security,start,end,source):
    if source.startswith('Nasdaq'):
        query=urlencode(dict(assetclass='stocks',fromdate=str(start),todate=str(end),limit=5000))
        request=Request(f'https://api.nasdaq.com/api/quote/{quote(security["symbol"],safe="")}/historical?{query}',headers={'User-Agent':'Mozilla/5.0','Accept':'application/json','Referer':'https://www.nasdaq.com/'})
        with urlopen(request,timeout=18) as response:return parse_nasdaq(json.load(response),security['symbol'])
    if source.startswith('Yahoo'):
        import yfinance as yf
        with _download_lock:
            frame=yf.Ticker(yahoo_symbol(security)).history(start=str(start),end=str(end),interval='1d',auto_adjust=True,raise_errors=True,timeout=15)
        if frame is None or frame.empty:raise ValueError('Yahoo增量行情为空')
        return [dict(date=str(index.date()),**{k.lower():float(row[k]) for k in ('Open','High','Low','Close','Volume')}) for index,row in frame.iterrows()]
    raise ValueError('旧缓存来源未知，需完整重新核对')


def read_valid_cache(security,cache):
    alias=yahoo_symbol(security)
    path=Path(cache)/'history'/f'{alias.encode().hex()}.json'
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        if payload.get('symbol')!=alias:raise ValueError('缓存代码不匹配')
        # Validate prices even when old; old real bars remain useful for charts.
        last=dt.date.fromisoformat(payload['rows'][-1]['date'])
        payload['rows']=validate(payload['rows'],last+dt.timedelta(days=1))
        return path,payload
    except (OSError,ValueError,KeyError,TypeError,IndexError):return path,None


def update_history(security,cache,today,required_date,fetch=fetch_range,full=load_history):
    path,old=read_valid_cache(security,cache)
    if old and old['rows'][-1]['date']>=str(required_date) and old.get('requested_history_days',0)>=HISTORY_DAYS:
        return dict(status='cached',symbol=security['symbol'],asof=old['rows'][-1]['date'],added=0)
    if not old or old.get('requested_history_days',0)<HISTORY_DAYS:
        data=full(security,cache,today=today)
        if data.get('warning'):raise RuntimeError(data['warning'])
        if data['rows'][-1]['date']<str(required_date):raise ValueError('完整下载尚未覆盖选股日期')
        return dict(status='initial',symbol=security['symbol'],asof=data['rows'][-1]['date'],added=len(data['rows']))
    anchor=old['rows'][-1]
    # Only one already cached bar is fetched to confirm source/adjustment consistency.
    incoming=fetch(security,dt.date.fromisoformat(anchor['date']),today,old['source'])
    dates=[r['date'] for r in incoming]
    if dates!=sorted(set(dates)):raise ValueError('增量日期重复或乱序')
    overlap=next((r for r in incoming if r['date']==anchor['date']),None)
    if overlap is None:raise ValueError('增量行情缺少用于核对的重叠日，未拼接不同口径数据')
    compatible=all(isinstance(overlap.get(k),(int,float)) and math.isfinite(overlap[k]) and abs(overlap[k]-anchor[k])<=max(1e-6,abs(anchor[k])*1e-5) for k in ('open','high','low','close'))
    if not compatible:
        # Split/dividend corrections can change old prices; never stitch incompatible series.
        data=full(security,cache,force=True,today=today)
        if data.get('warning') or data['rows'][-1]['date']<str(required_date):raise ValueError('复权口径变化，完整重新核对未成功')
        return dict(status='adjustment_refresh',symbol=security['symbol'],asof=data['rows'][-1]['date'],added=0)
    new=[r for r in incoming if anchor['date']<r['date']<str(today)]
    merged=validate(old['rows']+new,today)
    if merged[-1]['date']<str(required_date):raise ValueError('增量行情尚未覆盖选股日期')
    payload=dict(old,rows=merged,session_date=str(today),fetched_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    temporary.replace(path)
    return dict(status='incremental',symbol=security['symbol'],asof=merged[-1]['date'],added=len(new))
