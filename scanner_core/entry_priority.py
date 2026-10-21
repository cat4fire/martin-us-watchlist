"""Transparent daily setup heuristics, NOT a Martin-authored scoring model or live signal."""
import datetime as dt
import math
from zoneinfo import ZoneInfo
from dataclasses import dataclass

@dataclass(frozen=True)
class SetupParameters:
    near_ema_adr: float=.5
    tight_day_adr: float=.6
    tight_three_day_adr: float=1.5
    max_reference_risk: float=5.0

DEFAULTS=SetupParameters()

def unassessed(reason='待分析'):
    return dict(setup_tier=9,setup_status=reason,setup_distance=None,setup_trigger=None,
                setup_invalidation=None,setup_risk=None,setup_asof=None,setup_reason=reason)

def assess_setup(payload,today=None,parameters=DEFAULTS):
    today=today or dt.datetime.now(ZoneInfo('America/New_York')).date()
    rows=[r for r in payload.get('rows',[]) if not r.get('display_only') and not r.get('is_partial')]
    if len(rows)<50: return unassessed('不足50根日线')
    last,prior=rows[-1],rows[-2]
    keys=('open','high','low','close','ema9','ema21','ema50')
    if any(not isinstance(last.get(k),(int,float)) or not math.isfinite(last[k]) for k in keys):
        return unassessed('日线或EMA数据缺失')
    if last['low']<=0 or last['high']<last['low'] or last['close']<=0:
        return unassessed('日线数据无效')
    date=dt.date.fromisoformat(last['date'])
    if date>=today or (today-date).days>4 or payload.get('refresh_needed') or payload.get('warning'):
        return dict(unassessed('日线时效待确认'),setup_asof=last['date'])
    adr=sum((r['high']/r['low']-1)*100 for r in rows[-20:])/20
    if not math.isfinite(adr) or adr<=0: return unassessed('波动数据不足')
    inside=last['high']<=prior['high'] and last['low']>=prior['low']
    day_range=(last['high']/last['low']-1)*100
    three_high=max(r['high'] for r in rows[-3:])
    three_low=min(r['low'] for r in rows[-3:])
    three_tight=(three_high/three_low-1)*100<=adr*parameters.tight_three_day_adr
    day_tight=day_range<=adr*parameters.tight_day_adr
    tight=inside or day_tight or three_tight
    supports=[]
    for k in ('ema9','ema21'):
        if last[k]>0 and last['close']>=last[k] and (last['low']<=last[k] or (last['close']/last[k]-1)*100<=adr*parameters.near_ema_adr):
            supports.append(k.upper())
    healthy=last['close']>last['ema21'] and last['ema21']>last['ema50']
    use_three=three_tight and not inside and not day_tight
    trigger=three_high if use_three else last['high']
    invalidation=three_low if use_three else last['low']
    distance=(trigger/last['close']-1)*100
    risk=(trigger-invalidation)/trigger*100
    # Lexicographic ranking: structure first, then volatility-normalized distance and reference risk.
    if healthy and supports and tight and 0<risk<=parameters.max_reference_risk:
        tier,status=1,'结构就绪·待突破'
    elif healthy and supports:
        tier,status=2,'支撑附近·待收紧'
    elif healthy:
        tier,status=3,'趋势保留·等回调'
    else:
        tier,status=4,'趋势条件不足'
    reasons=[]
    if inside: reasons.append('内包日')
    if day_range<=adr*parameters.tight_day_adr: reasons.append('单日振幅收窄')
    if three_tight: reasons.append('3日区间紧凑')
    if supports: reasons.append('接近/守住'+','.join(supports))
    if not healthy: reasons.append('未同时满足收盘>EMA21>EMA50')
    if risk>parameters.max_reference_risk: reasons.append('结构参考风险>5%')
    return dict(setup_tier=tier,setup_status=status,setup_distance=distance,
                setup_trigger=trigger,setup_invalidation=invalidation,setup_risk=risk,
                setup_asof=last['date'],setup_source=payload.get('source'),setup_reason='；'.join(reasons) or '未形成所定义的紧凑支撑结构',
                setup_adr=adr,setup_normalized_distance=distance/adr)

def prioritize(rows,assessments):
    enriched=[dict(r,**assessments.get(r['symbol'],unassessed())) for r in rows]
    return sorted(enriched,key=lambda r:(r['setup_tier'],r.get('setup_normalized_distance',float('inf')),r['setup_risk'] if r['setup_risk'] is not None else float('inf'),r['symbol']))
