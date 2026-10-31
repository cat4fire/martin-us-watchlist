"""On-demand adjusted daily history with explicit source, session filtering and cache."""
import datetime as dt
import json
import math
import threading
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode,quote
from urllib.request import Request,urlopen

_download_lock=threading.Lock()
_symbol_locks={}
_symbol_locks_guard=threading.Lock()
HISTORY_DAYS=6*366

def parse_nasdaq(payload,symbol):
    data=payload.get('data')
    if not isinstance(data,dict) or str(data.get('symbol','')).upper()!=symbol.upper():
        raise ValueError('Nasdaq未返回对应股票的历史行情')
    table=data.get('tradesTable') or {}
    raw=table.get('rows')
    if not isinstance(raw,list) or not raw: raise ValueError('Nasdaq历史行情为空')
    count=data.get('totalRecords')
    if count is not None and int(str(count).replace(',',''))>len(raw):
        raise ValueError('Nasdaq历史行情被截断')
    def number(value,optional=False):
        text=str(value).replace('$','').replace(',','').strip()
        if optional and text.upper() in ('N/A','NA','NONE','NULL','','--','-'):
            return None
        return float(text)
    rows=[]
    for r in raw:
        try:
            rows.append(dict(date=str(dt.datetime.strptime(r['date'],'%m/%d/%Y').date()),**{k:number(r.get(k),optional=k=='volume') for k in ('open','high','low','close','volume')}))
        except (ValueError,TypeError) as exc:
            raise ValueError(f'Nasdaq {r.get("date", "未知日期")}价格或数值格式无效：{exc}') from exc
    rows.sort(key=lambda r:r['date'])
    return rows

def fetch_nasdaq(symbol,today):
    query=urlencode(dict(assetclass='stocks',fromdate=str(today-dt.timedelta(days=HISTORY_DAYS)),todate=str(today),limit=5000))
    url=f'https://api.nasdaq.com/api/quote/{quote(symbol,safe="")}/historical?{query}'
    request=Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json','Referer':'https://www.nasdaq.com/'})
    with urlopen(request,timeout=18) as response:
        payload=json.loads(response.read().decode('utf-8'))
    return parse_nasdaq(payload,symbol)

def ema_series(values,length):
    if not values: return []
    result=[values[0]]
    for value in values[1:]: result.append(result[-1]+2/(length+1)*(value-result[-1]))
    return result

def validate(rows,today):
    valid=[]
    previous=None
    for r in rows:
        date=dt.date.fromisoformat(r['date'])
        if previous is not None and date<=previous: raise ValueError('历史行情日期重复或乱序')
        previous=date
        if date>=today: continue  # Never present a partial daily bar as final.
        if any(not isinstance(r.get(k),(int,float)) or not math.isfinite(r[k]) for k in ('open','high','low','close')):
            raise ValueError('历史行情存在缺失值')
        volume=r.get('volume')
        if 'volume' not in r or (volume is not None and (not isinstance(volume,(int,float)) or not math.isfinite(volume) or volume<0)):
            raise ValueError('历史成交量无效')
        if r['low']<=0 or r['low']>min(r['open'],r['close']) or r['high']<max(r['open'],r['close']):
            raise ValueError(f'历史行情OHLCV无效：{r["date"]} O={r["open"]} H={r["high"]} L={r["low"]} C={r["close"]}')
        valid.append(r)
    if len(valid)<2: raise ValueError('没有足够的完整日线')
    if (today-dt.date.fromisoformat(valid[-1]['date'])).days>7:
        raise ValueError('历史行情超过7个自然日未更新；可能停牌或代码映射错误')
    return valid


def validated_history(rows,today):
    """Quarantine a few clearly invalid OLD bars, keeping original values and disclosure.

    Never repair prices by clamping them, remove recent bars, or stitch sources.
    """
    bad=[]
    for row in rows:
        date=dt.date.fromisoformat(row['date'])
        if date>=today:continue
        values=[row.get(k) for k in ('open','high','low','close')]
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in values):
            continue  # Strict validator reports missing/nonfinite prices; do not hide them.
        if row['low']<=0 or row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):bad.append(row)
    if not bad:return validate(rows,today),[]
    # Keep validation strict for recent or widespread corruption.
    if len(bad)>3 or len(bad)/max(1,len(rows))>.01 or any((today-dt.date.fromisoformat(r['date'])).days<=365 for r in bad):
        return validate(rows,today),[]
    dates={r['date'] for r in bad}
    retained=[r for r in rows if r['date'] not in dates]
    valid=validate(retained,today)
    if len(valid)<252:return validate(rows,today),[]
    # Verify ordering/duplicate dates in the original stream as well.
    if any(a['date']>=b['date'] for a,b in zip(rows,rows[1:])):raise ValueError('历史行情日期重复或乱序')
    return valid,[dict(r,reason='开高低收关系矛盾；原始数值保留用于审计，未修改价格') for r in bad]

def prepare(rows,today,display_days=365):
    rows=validate(rows,today)
    close=[r['close'] for r in rows]
    curves={n:ema_series(close,n) for n in (9,21,50)}
    cutoff=today-dt.timedelta(days=display_days) if display_days is not None else dt.date.min
    # Compute on warm-up bars before trimming the displayed year.
    return [dict(r,ema9=curves[9][i],ema21=curves[21][i],ema50=curves[50][i]) for i,r in enumerate(rows) if dt.date.fromisoformat(r['date'])>=cutoff]

def yahoo_symbol(security):
    symbol=security['symbol']
    # Boeing Series A depositary preferred; never substitute common stock BA.
    if symbol=='BA$A':return 'BA-PA'
    # Restrict automatic history to unambiguous common/class symbols. Do not map a preferred to its common.
    if '$' in symbol or symbol.endswith(('.W','.U','.R')) or '/P' in symbol:
        raise ValueError('此特殊证券的历史代码需单独核对，未自动替换为普通股')
    return symbol.replace('.','-')

def previous_weekday(today):
    day=today-dt.timedelta(days=1)
    while day.weekday()>4: day-=dt.timedelta(days=1)
    return day

def read_cached_history(security,cache_dir,today=None):
    """Fast local read; stale data is explicit and never waits for a network worker."""
    today=today or dt.datetime.now(ZoneInfo('America/New_York')).date()
    symbol=yahoo_symbol(security)
    path=Path(cache_dir)/'history'/f'{symbol.encode().hex()}.json'
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        if payload.get('symbol')!=symbol: return None
        rows=prepare(payload['rows'],today,display_days=None)
        if len(rows)<2: return None
        # Complete daily bars do not change intraday. Calendar holidays can trigger a harmless refresh.
        fresh=payload.get('session_date')==str(today) or dt.date.fromisoformat(rows[-1]['date'])>=previous_weekday(today)
        expanded=payload.get('requested_history_days',0)>=HISTORY_DAYS
        return dict(payload,rows=rows,cached=True,refresh_needed=not fresh or not expanded,
                    warning='' if fresh and expanded else '先显示本地缓存，正在后台更新/补齐约6年历史')
    except (OSError,ValueError,KeyError,TypeError): return None

def _load_history(security,cache_dir,force=False,today=None,fetch=None):
    today=today or dt.datetime.now(ZoneInfo('America/New_York')).date()
    symbol=yahoo_symbol(security)
    path=Path(cache_dir)/'history'/f'{symbol.encode().hex()}.json'
    cached=None
    if path.exists():
        try:
            cached=json.loads(path.read_text(encoding='utf-8'))
            if cached.get('symbol')!=symbol: cached=None
        except (ValueError,OSError): pass
    now=dt.datetime.now(dt.timezone.utc)
    if cached and not force:
        try:
            local=read_cached_history(security,cache_dir,today)
            if local and not local['refresh_needed']:
                return local
        except (ValueError,KeyError,TypeError): pass
    try:
        source='Nasdaq公开历史行情'
        adjustment='供应商OHLC；未额外做本地复权，分红/拆股口径需留意'
        if fetch is None:
            try:
                rows=fetch_nasdaq(security['symbol'],today)
                rows,quarantined=validated_history(rows,today)
            except Exception as nasdaq_error:
                import yfinance as yf
                try:
                    with _download_lock:
                        frame=yf.Ticker(symbol).history(start=str(today-dt.timedelta(days=HISTORY_DAYS)),end=str(today),interval='1d',auto_adjust=True,raise_errors=True,timeout=15)
                    if frame is None or frame.empty: raise ValueError('Yahoo未返回历史行情')
                    rows=[dict(date=str(index.date()),**{k.lower():float(r[k]) for k in ('Open','High','Low','Close','Volume')}) for index,r in frame.iterrows()]
                    rows,quarantined=validated_history(rows,today)
                    source='Yahoo Finance / yfinance（Nasdaq不可用时回退）'
                    adjustment='拆股与股息复权（auto_adjust）'
                except Exception as yahoo_error:
                    raise RuntimeError(f'Nasdaq: {nasdaq_error}；Yahoo: {yahoo_error}') from yahoo_error
        else:
            rows=fetch(symbol)
            rows,quarantined=validated_history(rows,today)
        rows=validate(rows,today)
        quality_warning=('历史数据缺口：隔离'+str(len(quarantined))+'根一年以前的异常日K（'+','.join(r['date'] for r in quarantined)+'）；未修改价格或补造K线') if quarantined else ''
        payload=dict(symbol=symbol,source=source,adjustment=adjustment,fetched_utc=now.isoformat(),session_date=str(today),requested_history_days=HISTORY_DAYS,rows=rows,quarantined_bars=quarantined,quality_warning=quality_warning)
        displayed=prepare(rows,today,display_days=None)
        if len(displayed)<2: raise ValueError('没有足够历史行情')
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
        temporary.replace(path)
        return dict(payload,rows=displayed,cached=False,warning='')
    except Exception as exc:
        if cached:
            try:
                displayed=prepare(cached['rows'],today,display_days=None)
                if len(displayed)>=2:
                    return dict(cached,rows=displayed,cached=True,warning=f'联网失败，显示缓存，非最新下载：{exc}')
            except (ValueError,KeyError,TypeError): pass
        raise RuntimeError(f'{symbol}历史行情获取失败：{exc}。可稍后重试；不以模拟行情替代。') from exc

def load_history(security,cache_dir,force=False,today=None,fetch=None):
    key=(str(Path(cache_dir).resolve()),security['symbol'])
    with _symbol_locks_guard:
        lock=_symbol_locks.setdefault(key,threading.Lock())
    with lock:
        return _load_history(security,cache_dir,force,today,fetch)
