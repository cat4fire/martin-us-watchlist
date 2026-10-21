"""Resolve native screener bars to a completed NY daily session, freeze per session."""
import datetime as dt
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
DAILY_FIELDS=('close','change','Volatility.W','EMA9','EMA21','EMA50','Value.Traded')

def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def bar_date(value):
    if not finite(value) or value<=0: return None
    try:return dt.datetime.fromtimestamp(value,NY).date()
    except (ValueError,OverflowError,OSError):return None

def completed_bar(row,now):
    now=now.astimezone(NY)
    current=bar_date(row.get('time'))
    if current is None or current>now.date(): return None
    # 15-minute grace avoids treating a delayed closing print as the final bar.
    use_current=current<now.date() or now.time()>=dt.time(16,15)
    suffix='' if use_current else '[1]'
    date=bar_date(row.get('time'+suffix))
    if date is None or date>now.date() or (date==now.date() and not use_current): return None
    if (now.date()-date).days>7: return None
    result={key:row.get(key+suffix) for key in DAILY_FIELDS}
    cap=row.get('market_cap_basic')
    # The feed lacks historical market cap. Scale current cap to the selected close;
    # this assumes unchanged share count, and the approximation is explicitly labelled.
    if suffix and finite(cap) and finite(result['close']) and finite(row.get('close')) and row['close']>0:
        cap=cap*result['close']/row['close']
    result.update(market_cap_basic=cap,scan_date=str(date),scan_basis='完整交易日',
                  cap_basis='当前披露股数按所选收盘价估算；非历史精确股数' if suffix else '供应商市值快照')
    return result

def freeze_completed(rows,cache_dir,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    candidates={row['ticker']:completed_bar(row,now) for row in rows}
    dates=[value['scan_date'] for value in candidates.values() if value]
    if not dates:
        return [dict(row,completed=None,completed_reason='缺少可验证的完整日K时间戳') for row in rows]
    session=max(dates)
    path=Path(cache_dir)/'potent_completed'/f'{session}.json'
    frozen={}
    if path.exists():
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema')==1 and data.get('session')==session:
                frozen=data['bars']
        except (ValueError,KeyError,OSError):pass
    changed=False
    for ticker,value in candidates.items():
        if not value or value['scan_date']!=session or ticker in frozen: continue
        if all(finite(value.get(k)) for k in DAILY_FIELDS+('market_cap_basic',)):
            frozen[ticker]=value
            changed=True
    if changed:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(schema=1,session=session,frozen_utc=now.isoformat(),bars=frozen),ensure_ascii=False),encoding='utf-8')
        temporary.replace(path)
    result=[]
    for row in rows:
        value=frozen.get(row['ticker'])
        candidate=candidates[row['ticker']]
        if value is None and candidate and candidate['scan_date']==session: value=candidate
        result.append(dict(row,completed=value,completed_reason='' if value else f'缺少{session}完整日数据（停牌/延迟/字段缺失）'))
    return result
