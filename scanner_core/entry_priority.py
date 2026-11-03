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

def _assess_setup(payload,today=None,parameters=DEFAULTS):
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


def assess_setup(payload,today=None,parameters=DEFAULTS):
    result=_assess_setup(payload,today,parameters)
    fields=dict(entry_structure='暂未识别',entry_trigger=None,entry_invalidation=None,entry_distance=None,entry_risk=None,entry_asof=None,entry_state='待分析',entry_basis='至少需要51根完整日线；辅助规则，非Martin固定公式')
    rows=[r for r in payload.get('rows',[]) if not r.get('display_only') and not r.get('is_partial')]
    if len(rows)<51:return dict(result,**fields)
    # Freeze the setup before the latest completed session; that session only evaluates it.
    prior=_assess_setup(dict(payload,rows=rows[:-1]),today,parameters)
    if prior.get('setup_trigger') is None:return dict(result,**fields)
    a,b=rows[-2],rows[-3];reason=prior.get('setup_reason','')
    if '内包日' in reason:kind='内包日'
    elif '单日振幅收窄' in reason or '3日区间紧凑' in reason:kind='收紧'
    elif '接近/守住' in reason and prior.get('setup_tier',9)<=2:kind='回调支撑'
    else:return dict(result,**dict(fields,entry_asof=a['date']))
    trigger=prior['setup_trigger'];stop=prior['setup_invalidation'];last=rows[-1]
    hit=last['high']>trigger;failed=last['low']<stop
    state='同日双触及·顺序未知' if hit and failed else '结构失效' if failed else '已突破' if hit else '等待触发'
    fields.update(entry_structure=kind,entry_trigger=trigger,entry_invalidation=stop,entry_distance=(trigger/last['close']-1)*100,entry_risk=(trigger-stop)/trigger*100,entry_asof=a['date'],entry_state=state,entry_basis=f"结构截至{a['date']}；用{last['date']}完整日高低价检查；参考位不使用该检查日数据。辅助规则，非Martin固定公式")
    return dict(result,**fields)


def trade_annotations(rows,parameters=DEFAULTS,start_date=None):
    """Causal daily structure simulation; not actual fills or historical pool membership."""
    events=[];segments=[];position=None;exit_next=False
    for i in range(50,len(rows)):
        bar=rows[i];previous=rows[i-1]
        if start_date and bar['date']<start_date:continue
        if position is not None:
            stop=position['stop']
            if exit_next or bar['low']<=stop:
                # If a scheduled next-open exit gaps below the stop, the open is used.
                price=bar['open'] if exit_next or bar['open']<=stop else stop
                reason='前日收盘跌破EMA21，次日开盘退出' if exit_next else '触及初始结构止损；跳空越过时按开盘价'
                events.append(dict(date=bar['date'],kind='sell',price=price,reason=reason))
                segments.append(dict(start=position['date'],end=bar['date'],price=stop))
                position=None;exit_next=False
            else:exit_next=bar['close']<bar['ema21']
            continue
        setup=_assess_setup({'rows':rows[max(0,i-60):i]},dt.date.fromisoformat(bar['date'])+dt.timedelta(days=1),parameters)
        if setup.get('setup_tier')!=1:continue
        trigger=setup['setup_trigger'];stop=setup['setup_invalidation']
        if bar['high']<=trigger:continue
        price=max(bar['open'],trigger)
        risk=(price-stop)/price*100
        if not 0<risk<=parameters.max_reference_risk:continue
        if bar['low']<=stop:
            events.append(dict(date=bar['date'],kind='uncertain',price=price,stop=stop,reason=f"前日结构触发{trigger:.2f}、止损{stop:.2f}同日双触及；无法判断先后，本模拟跳过该次入场"))
            continue
        position=dict(date=bar['date'],stop=stop);exit_next=bar['close']<bar['ema21']
        events.append(dict(date=bar['date'],kind='buy',price=price,stop=stop,reason=f"结构截至{previous['date']}；{setup['setup_reason']}；突破{trigger:.2f}，初始止损{stop:.2f}，参考风险{risk:.2f}%。仅日线假设成交"))
    if position:segments.append(dict(start=position['date'],end=rows[-1]['date'],price=position['stop']))
    return dict(version=1,events=events,stops=segments,notice='程序辅助策略，非Martin完整交易系统；历史结构模拟，不代表历史通过Potent筛选或真实成交；同日双触及跳过入场；不含费用和滑点。')
