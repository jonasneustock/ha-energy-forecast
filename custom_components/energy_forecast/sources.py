"""Read installed HA integrations instead of implementing new cloud clients."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

from .engine import grid, normalize_prices, spread_periods, time_parse

_LOGGER = logging.getLogger(__name__)


def numeric_state(hass, entity_id):
    state = hass.states.get(entity_id) if entity_id else None
    try:
        return float(state.state) if state and state.state not in ('unknown', 'unavailable') else None
    except (ValueError, TypeError):
        return None


async def fetch_sources(hass, settings, now):
    """Combine only actual Forecast.Solar configuration entries (not derived sensors)."""
    slots = grid(now)
    prices, solar = [None] * len(slots), [0.0] * len(slots)
    issues = []
    try:
        data = await hass.services.async_call('tibber', 'get_prices', {
            'start': slots[0].isoformat(), 'end': (slots[-1] + timedelta(minutes=15)).isoformat()
        }, blocking=True, return_response=True)
        homes = (data or {}).get('prices', {})
        home = settings.get('tibber_home')
        if home and home not in homes:
            raise ValueError('Selected Tibber home is not available')
        if not home and len(homes) != 1:
            raise ValueError('Choose Tibber home; multiple or no homes returned')
        prices = normalize_prices(homes[home or next(iter(homes))], slots)
        if all(p is None for p in prices):
            issues.append('No current/future Tibber prices returned')
    except Exception as err:
        _LOGGER.warning('Cannot fetch Tibber prices: %s', err)
        issues.append(f'Tibber: {err}')
    entries = hass.config_entries.async_entries('forecast_solar')
    if not entries:
        issues.append('No Forecast.Solar configuration entry installed')
    fingerprints = set()
    for entry in entries:
        fingerprint = json.dumps({'data':dict(entry.data),'options':dict(entry.options)}, sort_keys=True, default=str)
        if fingerprint in fingerprints:
            issues.append(f'Identical Forecast.Solar configuration skipped: {entry.title}')
            continue
        fingerprints.add(fingerprint)
        try:
            data = await hass.services.async_call('forecast_solar', 'get_forecast', {
                'config_entry': entry.entry_id, 'start': slots[0].isoformat(),
                'end': (slots[-1] + timedelta(minutes=15)).isoformat(), 'resolution': 'raw'
            }, blocking=True, return_response=True)
            energy = (data or {}).get('wh_period', {})
            if not energy:
                raise ValueError('No period energy in forecast')
            times = sorted(time_parse(k) for k in energy)
            spacing = (times[1]-times[0]).total_seconds()/60 if len(times)>1 else 60
            values = spread_periods(energy, slots, default_minutes=spacing)
            for idx, value in enumerate(values):
                solar[idx] += value / 1000
        except Exception as err:
            _LOGGER.warning('Forecast.Solar entry %s failed: %s', entry.entry_id, err)
            issues.append(f'Forecast.Solar {entry.title}: {err}')
    load_sensor = settings.get('load_sensor')
    remaining = numeric_state(hass, load_sensor)
    if remaining is None and load_sensor:
        issues.append('Household remaining-day load forecast unavailable')
    if remaining is not None and not 0 <= remaining <= 1000:
        issues.append('Household load forecast out of expected kWh range')
        remaining = None
    pv_remaining = sum(s for t,s in zip(slots,solar) if t>=now and
        t.astimezone(now.tzinfo).date() == now.date())
    return {'slots': slots, 'price': prices, 'solar': solar, 'load_kwh': remaining,
            'pv_remaining_kwh': pv_remaining, 'issues': issues, 'last_update': now.isoformat(),
            'source_entries': len(entries)}
