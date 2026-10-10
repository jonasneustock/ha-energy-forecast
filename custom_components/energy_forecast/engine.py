"""Pure, dependency-free quarter-hour rule planner (also used in backtests)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil, isfinite

STEP = timedelta(minutes=15)
SLOTS = 192


def time_parse(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Forecast timestamp is not timezone-aware')
    return dt.astimezone(timezone.utc)


def grid(now):
    start = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 15)
    return [start + i * STEP for i in range(SLOTS)]


def spread_periods(periods, slots, value_name='value', default_minutes=60):
    """Allocate energy/power across time intervals without changing total energy."""
    ordered = sorted([(time_parse(k), float(v)) for k, v in periods.items() if v is not None])
    output = [0.0] * len(slots)
    for i, (start, value) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else start + timedelta(minutes=default_minutes)
        duration = (end-start).total_seconds()
        if not 0 < duration <= 86400 or not isfinite(value):
            continue
        for j, slot in enumerate(slots):
            overlap = max(0, (min(slot + STEP, end) - max(slot, start)).total_seconds())
            if overlap:
                output[j] += value * overlap / duration
    return output


def price_slots(prices, slots):
    ordered = sorted([(time_parse(p['start_time']), float(p['price'])) for p in prices if p.get('price') is not None])
    result = [None] * len(slots)
    for i, (start, price) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else start + (ordered[-1][0]-ordered[-2][0] if len(ordered) > 1 else timedelta(minutes=15))
        if not isfinite(price) or (end - start) > timedelta(hours=3):
            continue
        for j, slot in enumerate(slots):
            if start <= slot and slot + STEP <= end:
                result[j] = price
    return result


def normalize_prices(prices, slots):
    """No fill-forward past the end of a published tariff period."""
    return price_slots(prices, slots)


def _numeric(a, operator, b):
    try:
        a, b = float(a), float(b)
        return isfinite(a) and isfinite(b) and {'<': lambda: a < b, '<=': lambda: a <= b,
            '>': lambda: a > b, '>=': lambda: a >= b, '==': lambda: a == b, '!=': lambda: a != b}[operator]()
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def matches(node, row, context):
    """Evaluate safe declarative nested conditions; no eval, no Jinja."""
    if not node:
        return True
    op = node.get('op', 'condition')
    children = node.get('children', [])
    if op == 'and':
        return all(matches(child, row, context) for child in children)
    if op == 'or':
        return bool(children) and any(matches(child, row, context) for child in children)
    if op == 'not':
        return len(children) == 1 and not matches(children[0], row, context)
    kind, value, comparator = node.get('type'), node.get('value'), node.get('operator', '>=')
    if kind == 'entity':
        current = context.get('states', {}).get(node.get('entity_id'))
        if current in (None, 'unknown', 'unavailable'):
            return False
        if comparator in ('==', '!=') and not _can_number(value):
            return (str(current) == str(value)) if comparator == '==' else (str(current) != str(value))
        return _numeric(current, comparator, value)
    if kind == 'price_level':
        if context.get('states', {}).get(node.get('entity_id')) in (None, 'unknown', 'unavailable'):
            return False
        return str(context.get('states', {}).get(node.get('entity_id'))) == str(value)
    if kind == 'price':
        return _numeric(row.get('price'), comparator, value)
    if kind == 'rank_percent':
        return _numeric(row.get('rank_percent'), comparator, value)
    if kind == 'price_spread':
        return _numeric(row.get('spread'), comparator, value)
    if kind == 'solar_power':
        return _numeric(row.get('pv_kwh', 0)*4, comparator, value)
    if kind == 'solar_remaining':
        return _numeric(context.get('solar_remaining'), comparator, value)
    if kind == 'surplus_remaining':
        return _numeric(context.get('surplus_remaining'), comparator, value)
    if kind == 'forecast_error':
        return _numeric(context.get('forecast_error_pct'), comparator, value)
    if kind == 'solar_before':
        # node.value is threshold kWh, node.deadline is HH:MM local time
        hour = node.get('deadline', '18:00')
        try:
            cutoff = datetime.strptime(hour, '%H:%M').time()
            return _numeric(sum(x['pv_kwh'] for x in context['rows'] if
                x['time'].astimezone(context['local_tz']).date() == row['time'].astimezone(context['local_tz']).date()
                and x['time'].astimezone(context['local_tz']).time() < cutoff), comparator, value)
        except (ValueError, KeyError):
            return False
    return False  # unknown conditions fail closed


def _can_number(v):
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def validate_condition(node, depth=0):
    if depth > 8 or not isinstance(node, dict):
        raise ValueError('Invalid condition depth or format')
    op = node.get('op', 'condition')
    if op in ('and', 'or', 'not'):
        children = node.get('children')
        if not isinstance(children, list) or len(children) > 20 or (op == 'not' and len(children) != 1):
            raise ValueError('Invalid condition group')
        for child in children:
            validate_condition(child, depth+1)
    elif op == 'condition':
        if node.get('type') not in ('entity', 'price_level', 'price', 'rank_percent', 'price_spread',
                                   'solar_power', 'solar_remaining', 'surplus_remaining', 'forecast_error', 'solar_before'):
            raise ValueError('Unknown condition type')
        if node.get('operator', '>=') not in ('<', '<=', '>', '>=', '==', '!='):
            raise ValueError('Invalid comparison operator')
        if node.get('type') in ('entity', 'price_level') and not isinstance(node.get('entity_id'), str):
            raise ValueError('Missing HA entity')
    else:
        raise ValueError('Unknown group operation')


def validate_rule(rule):
    if not isinstance(rule, dict) or not isinstance(rule.get('name'), str) or not 1 <= len(rule['name']) <= 80:
        raise ValueError('Rule name must be 1–80 characters')
    if rule.get('objective', 'min_cost') not in ('min_cost', 'most_solar', 'earliest', 'max_spread'):
        raise ValueError('Invalid objective')
    if not 1 <= int(rule.get('duration_slots', 4)) <= SLOTS:
        raise ValueError('Duration out of range')
    if not 0 <= int(rule.get('min_run_slots', 1)) <= int(rule.get('duration_slots', 4)):
        raise ValueError('Minimum runtime exceeds duration')
    if not 0 <= float(rule.get('power_kw', 0)) <= 100:
        raise ValueError('Power must be 0–100 kW')
    if not -1000 <= int(rule.get('priority', 0)) <= 1000:
        raise ValueError('Priority out of bounds')
    if rule.get('conditions'):
        validate_condition(rule['conditions'])
    if int(rule.get('min_off_slots',0)) < 0 or int(rule.get('max_run_slots',192)) < 1:
        raise ValueError('Invalid min-off or maximum runtime')
    if int(rule.get('max_run_slots',192)) < int(rule.get('duration_slots',4)):
        raise ValueError('Maximum runtime smaller than minimum duration')
    for key in ('earliest', 'deadline'):
        if rule.get(key):
            time_parse(rule[key])
    return rule


def plan(rule, rows, context):
    """Plan independently; return a time-window proposal, never actuate."""
    validate_rule(rule)
    needed = max(int(rule.get('duration_slots', 4)),
                 ceil(float(rule.get('energy_kwh', 0)) / (max(float(rule.get('power_kw', 0)), 0.00001) * .25)))
    if needed > len(rows) or needed > int(rule.get('max_run_slots', 192)):
        return None
    objective = rule.get('objective', 'min_cost')
    earliest = time_parse(rule['earliest']) if rule.get('earliest') else rows[0]['time']
    deadline = time_parse(rule['deadline']) if rule.get('deadline') else rows[-1]['time'] + STEP
    valid_prices = sorted(r['price'] for r in rows if r['price'] is not None)
    mean_price = sum(valid_prices)/len(valid_prices) if valid_prices else None
    def savings(cost, slots_count):
        return round(mean_price*float(rule.get('power_kw',0))*0.25*slots_count-cost,3) if cost is not None and mean_price is not None else None
    for row in rows:
        row['rank_percent'] = (100 * sum(v <= row['price'] for v in valid_prices) / len(valid_prices)) if row['price'] is not None and valid_prices else None
        row['spread'] = (max((x['price'] for x in rows if x['time'] > row['time'] and x['price'] is not None), default=row['price']) - row['price']) if row['price'] is not None else None
    def allowed(r):
        if r['time'] < earliest or r['time'] + STEP > deadline:
            return False
        begin, end = rule.get('allowed_start_hour'), rule.get('allowed_end_hour')
        if begin and end:
            h = r['time'].astimezone(context['local_tz']).strftime('%H:%M')
            if not (begin <= h < end if begin <= end else (h >= begin or h < end)):
                return False
        return matches(rule.get('conditions'),r,context)

    if rule.get('interruptible'):
        # Smallest sufficient heuristic: choose disjoint consecutive chunks,
        # with minimum run time and minimum off time between chunks.
        chunk = max(1, int(rule.get('min_run_slots',1)))
        count = ceil(needed / chunk)
        if count*chunk > int(rule.get('max_run_slots',192)):
            return None
        chunks=[]
        for i in range(len(rows)-chunk+1):
            window=rows[i:i+chunk]
            if not all(allowed(r) for r in window):
                continue
            if objective in ('min_cost','max_spread') and any(x['price'] is None for x in window):
                continue
            pv=sum(x['pv_kwh'] for x in window)
            cost=sum(float(rule.get('power_kw',0))*.25*x['price'] for x in window) if all(x['price'] is not None for x in window) else None
            score=(cost if cost is not None else float('inf')) if objective=='min_cost' else (-pv if objective=='most_solar' else (-sum((x['spread'] or 0) for x in window) if objective=='max_spread' else i))
            chunks.append((score,i))
        chosen=[]
        off=int(rule.get('min_off_slots',0))
        for _, i in sorted(chunks):
            if all(i+chunk+off<=j or j+chunk+off<=i for j in chosen):
                chosen.append(i)
                if len(chosen)==count:
                    break
        if len(chosen)<count:
            return None
        chosen.sort()
        segments=[{'start':rows[i]['time'].isoformat(),'end':(rows[i+chunk-1]['time']+STEP).isoformat()} for i in chosen]
        window=[r for i in chosen for r in rows[i:i+chunk]]
        cost=sum(float(rule.get('power_kw',0))*.25*x['price'] for x in window) if all(x['price'] is not None for x in window) else None
        return {'start':segments[0]['start'],'end':segments[-1]['end'],'segments':segments,
                'energy_kwh':round(len(window)*float(rule.get('power_kw',0))*.25,3),
                'forecast_pv_kwh':round(sum(x['pv_kwh'] for x in window),3),
                'estimated_grid_cost_eur':round(cost,3) if cost is not None else None,
                'estimated_savings_vs_mean_eur':savings(cost,len(window)),
                'slots':len(window),'reason':f'Optimized interruptible {objective} in {len(segments)} segments'}

    candidates = []
    for start in range(len(rows)-needed+1):
        window = rows[start:start+needed]
        if objective in ('min_cost', 'max_spread') and any(x['price'] is None for x in window):
            continue
        if not all(allowed(r) for r in window):
            continue
        pv = sum(x['pv_kwh'] for x in window)
        # Grid-only upper bound: remaining-day load forecast cannot establish hourly PV surplus.
        cost = sum(float(rule.get('power_kw', 0)) * .25 * x['price'] for x in window) if all(x['price'] is not None for x in window) else None
        spread = sum(x['spread'] or 0 for x in window) / len(window)
        score = (cost if cost is not None else float('inf')) if objective == 'min_cost' else (-pv if objective == 'most_solar' else (-spread if objective == 'max_spread' else start))
        candidates.append((score, start, pv, cost))
    if not candidates:
        return None
    score, start, pv, cost = min(candidates)
    return {'start': rows[start]['time'].isoformat(), 'end': (rows[start+needed-1]['time']+STEP).isoformat(),
            'segments':[{'start':rows[start]['time'].isoformat(),'end':(rows[start+needed-1]['time']+STEP).isoformat()}],
            'energy_kwh': round(float(rule.get('power_kw', 0)) * needed*.25, 3),
            'forecast_pv_kwh': round(pv, 3), 'estimated_grid_cost_eur': round(cost, 3) if cost is not None else None,  # grid-only upper bound
            'slots': needed, 'reason': f"Optimized {objective} across {len(rows)} quarter-hour intervals"}
