"""Exchange-wide discovery and evidence-limited Martin public workflow."""
import csv
import datetime as dt
import io
import json
import math
from pathlib import Path
from urllib.request import Request, urlopen

DIRECTORY_URLS = {
    'nasdaqlisted': 'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt',
    'otherlisted': 'https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt',
}

def parse_directory(text, source):
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter='|'):
        symbol = r.get('Symbol') if source == 'nasdaqlisted' else r.get('ACT Symbol')
        if not symbol or symbol.startswith('File Creation Time') or r.get('Test Issue') != 'N':
            continue
        # Keep ALL non-test securities; ETF/type filtering is an explicit UI option.
        name = r.get('Security Name', '')
        rows.append(dict(symbol=symbol, yahoo=(r.get('CQS Symbol') or symbol).replace('.', '-'),
                         name=name, exchange='NASDAQ' if source == 'nasdaqlisted' else r.get('Exchange', ''),
                         etf=r.get('ETF', 'N'), source=source))
    if not rows:
        raise ValueError(f'{source}: 股票目录为空或格式发生变化')
    return rows

def load_universe(cache_dir):
    """Never silently replace the full universe with a handpicked list or stale cache."""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    combined = {}
    fetched = dt.datetime.now(dt.timezone.utc).isoformat()
    for source, url in DIRECTORY_URLS.items():
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=30) as response:
            text = response.read().decode('utf-8-sig')
        rows = parse_directory(text, source)
        (directory / f'{source}.txt').write_text(text, encoding='utf-8')
        for r in rows:
            combined[r['symbol']] = r
    result = sorted(combined.values(), key=lambda r: r['symbol'])
    (directory / 'universe.json').write_text(json.dumps({'fetched_utc': fetched, 'securities': result}, ensure_ascii=False), encoding='utf-8')
    return result

def ema(values, length):
    value = values[0]
    for c in values[1:]:
        value += 2 / (length + 1) * (c - value)
    return value

def metrics(rows):
    """Rows must carry ISO date, High, Low, Close, Volume. No invented entry filters."""
    if len(rows) < 2:
        raise ValueError('不足2根完整日线')
    dates = [dt.date.fromisoformat(r['date']) for r in rows]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError('日期乱序或重复')
    for r in rows:
        if any(not math.isfinite(r[k]) for k in ('High', 'Low', 'Close', 'Volume')) or r['Low'] <= 0 or r['Volume'] < 0 or not r['Low'] <= r['Close'] <= r['High']:
            raise ValueError('行情存在缺失或无效值')
    c = [r['Close'] for r in rows]
    e9, e21, e50 = [ema(c, n) for n in (9, 21, 50)]
    group = '历史不足50根' if len(rows) < 50 else 'Lead' if e9 > e21 > e50 else 'Lag' if e9 < e21 < e50 else 'Mediocre' if (e9 > e21 < e50 or e9 < e21 > e50) else '均线相等/未分类'
    cutoff = dates[-1] - dt.timedelta(days=30)
    bases = [i for i, d in enumerate(dates[:-1]) if d <= cutoff]
    return dict(date=str(dates[-1]), close=c[-1], day_gain=(c[-1]/c[-2]-1)*100,
                gain30=(c[-1]/c[bases[-1]]-1)*100 if bases else None,
                adr20=sum((r['High']/r['Low']-1)*100 for r in rows[-20:])/20 if len(rows)>=20 else None,
                dollars20=sum(r['Close']*r['Volume'] for r in rows[-20:])/20 if len(rows)>=20 else None,
                group=group, ema9=e9, ema21=e21, ema50=e50,
                adr_preferred=None if len(rows)<20 else sum((r['High']/r['Low']-1)*100 for r in rows[-20:])/20>5,
                bars=len(rows))

def rank_results(results, key, adr_only=False, group='全部'):
    selected = [r for r in results if r.get(key) is not None and (not adr_only or r['adr_preferred'] is True) and (group == '全部' or r['group']==group)]
    return sorted(selected, key=lambda r: (-r[key], r['symbol']))

def scan(universe, asof, cache_dir, progress=lambda s: None, cancelled=lambda: False, download=None):
    if download is None:
        import yfinance as yf
        download = yf.download
    root = Path(cache_dir) / str(asof)
    root.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    visited = 0
    empty_batches = 0
    for offset in range(0, len(universe), 25):
        if cancelled():
            break
        batch = universe[offset:offset+25]
        pending = []
        for security in batch:
            # Directory symbols cannot become filesystem paths.
            filename = security['symbol'].encode('utf-8').hex() + '.json'
            path = root / filename
            if path.exists():
                try:
                    results.append(dict(security, **metrics(json.loads(path.read_text(encoding='utf-8')))))
                    visited += 1
                    continue
                except (ValueError, KeyError, TypeError):
                    pass
            pending.append((security, path))
        if not pending:
            progress(f'缓存恢复 {visited}/{len(universe)}')
            continue
        progress(f'行情批次 {offset+1}–{offset+len(batch)} / {len(universe)}；已处理 {visited}')
        try:
            data = download([s['yahoo'] for s, _ in pending], start=str(asof-dt.timedelta(days=420)), end=str(asof+dt.timedelta(days=1)), interval='1d', auto_adjust=True, group_by='ticker', multi_level_index=True, progress=False, threads=False, timeout=15)
        except Exception as exc:
            data = None
            errors.append({'symbol': '[batch]', 'reason': str(exc)})
        successful = 0
        for security, path in pending:
            if cancelled():
                break
            visited += 1
            try:
                if data is None or data.empty:
                    raise ValueError('数据源未返回行情，可能限流或网络失败')
                frame = data[security['yahoo']] if getattr(data.columns, 'nlevels', 1)>1 else data
                rows = [dict(date=str(index.date()), **{k: float(r[k]) for k in ('High','Low','Close','Volume')}) for index,r in frame.iterrows() if not r[['High','Low','Close','Volume']].isna().all()]
                if not rows or (asof-dt.date.fromisoformat(rows[-1]['date'])).days > 4:
                    raise ValueError('行情为空或过旧')
                value = metrics(rows)
                path.write_text(json.dumps(rows, allow_nan=False), encoding='utf-8')
                results.append(dict(security, **value))
                successful += 1
            except Exception as exc:
                errors.append({'symbol': security['symbol'], 'reason': str(exc)})
        empty_batches = empty_batches+1 if successful == 0 else 0
        if empty_batches >= 3:
            progress('连续3批没有有效行情，已停止请求。结果不完整；缓存可在下次扫描恢复。')
            break
    report = dict(asof=str(asof), universe=len(universe), attempted=visited, valid=len(results), failed=visited-len(results), unprocessed=len(universe)-visited, complete=visited==len(universe) and len(results)==len(universe), cancelled=cancelled(), results=results, errors=errors)
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
