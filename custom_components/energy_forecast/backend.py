"""Five-minute rule coordinator with HA-native event output and storage."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from statistics import median
from bisect import bisect_right
from uuid import uuid4
import re
import logging

from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import history

from .const import DOMAIN, EVENT, DEFAULT_LOAD, STORE_VERSION
from .engine import STEP, SLOTS, grid, plan, time_parse, validate_rule
from .sources import fetch_sources, numeric_state

_LOGGER = logging.getLogger(__name__)


class Manager:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.store = Store(hass, STORE_VERSION, f'{DOMAIN}_{entry.entry_id}')
        self.data = {'rules': [], 'settings': {'load_sensor': DEFAULT_LOAD,
            'pv_actual_sensor': '', 'curtailment_sensor': '', 'max_grid_kw': 0,
            'tibber_home': '', 'grace_minutes': 30, 'grid_import_sensor': ''}, 'states': {},
            'snapshots': [], 'observations': []}
        self.forecast = None
        self.diagnostics = []
        self._lock = asyncio.Lock()
        self.unsub = None
        self.manual_unsub = None

    @property
    def rules(self):
        return self.data['rules']

    def _publish(self, rule, phase, **kwargs):
        payload = {'rule_id': rule['id'], 'name': rule['name'], 'phase': phase,
                   'time': dt_util.utcnow().isoformat(), **kwargs}
        self.hass.bus.async_fire(EVENT, payload)

    def _signal(self):
        async_dispatcher_send(self.hass, f'{DOMAIN}_changed')

    async def start(self):
        stored = await self.store.async_load()
        if stored and isinstance(stored, dict):
            self.data.update({k: stored[k] for k in self.data if k in stored})
        self.unsub = async_track_time_interval(self.hass, self.evaluate, timedelta(minutes=5))
        self.manual_unsub = async_track_state_change_event(self.hass,
            [r['manual_entity_id'] for r in self.rules if r.get('manual_entity_id')], self._manual_change) if any(r.get('manual_entity_id') for r in self.rules) else None
        self.hass.async_create_task(self.evaluate())

    async def stop(self):
        if self.unsub:
            self.unsub()
        if self.manual_unsub:
            self.manual_unsub()
        await self.store.async_save(self.data)

    async def save_rule(self, item):
        item = dict(item)
        validate_rule(item)
        item['id'] = item.get('id') or uuid4().hex
        if not re.fullmatch(r'[0-9a-f]{32}', item['id']):
            raise ValueError('Invalid rule ID')
        if any(r['id'] == item['id'] for r in self.rules):
            idx = next(i for i,r in enumerate(self.rules) if r['id'] == item['id'])
            self.rules[idx] = item
        else:
            if len(self.rules) >= 100:
                raise ValueError('Maximum 100 rules')
            self.rules.append(item)
        await self.store.async_save(self.data)
        await self.hass.config_entries.async_reload(self.entry.entry_id)
        return item['id']

    async def remove_rule(self, rule_id):
        self.data['rules'] = [r for r in self.rules if r['id'] != rule_id]
        self.data['states'].pop(rule_id, None)
        await self.store.async_save(self.data)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    async def update_settings(self, settings):
        valid = {'load_sensor','pv_actual_sensor','curtailment_sensor','max_grid_kw',
                 'tibber_home','grace_minutes','load_includes_planned','grid_import_sensor'}
        if not isinstance(settings, dict) or set(settings) - valid:
            raise ValueError('Unknown setting')
        if not 0 <= float(settings.get('max_grid_kw', 0)) <= 200:
            raise ValueError('Invalid grid import cap')
        if not 0 <= float(settings.get('grace_minutes', 30)) <= 1440:
            raise ValueError('Invalid grace period')
        self.data['settings'].update(settings)
        await self.store.async_save(self.data)
        self._signal()

    def _manual_change(self, event):
        state = event.data.get('new_state')
        if not state or not state.context.user_id:
            return
        entity = event.data.get('entity_id')
        for rule in self.rules:
            if rule.get('manual_entity_id') == entity:
                duration = int(rule.get('override_minutes', 120))
                self.hass.async_create_task(self.override(rule['id'],duration))

    async def override(self, rule_id, minutes):
        if not any(r['id'] == rule_id for r in self.rules):
            raise ValueError('Unknown rule')
        state = self.data['states'].setdefault(rule_id, {})
        state['override_until'] = (dt_util.utcnow() + timedelta(minutes=max(0, min(int(minutes), 10080)))).isoformat()
        await self.store.async_save(self.data)
        self._signal()

    async def acknowledge(self, rule_id, status):
        if status not in ('started','completed','failed','stopped') or not any(r['id']==rule_id for r in self.rules):
            raise ValueError('Invalid acknowledgement')
        state = self.data['states'].setdefault(rule_id, {})
        state['acknowledged'] = status
        state['ack_time'] = dt_util.utcnow().isoformat()
        rule = next(r for r in self.rules if r['id'] == rule_id)
        self._publish(rule, 'acknowledged', status=status)
        await self.store.async_save(self.data)
        self._signal()

    def _dependencies(self, node):
        if not node:
            return set(), set()
        if node.get('op') in ('and','or','not'):
            deps, entities = set(), set()
            for child in node.get('children', []):
                a,b = self._dependencies(child)
                deps.update(a);entities.update(b)
            return deps, entities
        kind = node.get('type', '')
        deps = set()
        if kind in ('price','rank_percent','price_spread'):
            deps.add('price')
        if kind in ('solar_power','solar_remaining','surplus_remaining','solar_before','forecast_error'):
            deps.add('solar')
        if kind == 'surplus_remaining':
            deps.add('load')
        return deps, {node.get('entity_id')} if kind in ('entity','price_level') else set()

    def _correction(self):
        # Only learn if curtailment is measured: otherwise inverter output can
        # underestimate available solar at zero-export homes.
        if not self.data['settings'].get('curtailment_sensor'):
            return 1.0
        ratios = [x['actual']/x['expected'] for x in self.data['observations'][-500:]
                  if x.get('expected', 0) >= 0.05 and x.get('actual', -1) >= 0
                  and not x.get('curtailed', True)]
        return max(0.65, min(1.2, median(ratios))) if len(ratios) >= 16 else 1.0

    def _record(self, forecast, now):
        # Keep hourly issued forecasts for reconstructable backtesting.
        snapshots = self.data['snapshots']
        if not snapshots or (now - time_parse(snapshots[-1]['issued'])) >= timedelta(hours=1):
            snapshots.append({'issued': now.isoformat(),
                'times': [t.isoformat() for t in forecast['slots']],
                'prices': forecast['price'], 'solar': forecast['solar'],
                'load_kwh': forecast['load_kwh']})
        self.data['snapshots'] = [x for x in snapshots if time_parse(x['issued']) >= now-timedelta(days=30)]
        # Sampling actual PV power every 5 min gives interval comparison for learning.
        settings = self.data['settings']
        actual = numeric_state(self.hass, settings.get('pv_actual_sensor'))
        if actual is not None:
            unit = (self.hass.states.get(settings['pv_actual_sensor']).attributes.get('unit_of_measurement') or '').lower()
            kw = actual/1000 if unit == 'w' else actual if unit == 'kw' else None
            if kw is not None and 0 <= kw <= 100:
                observed = self.data['observations']
                curb = self.hass.states.get(settings.get('curtailment_sensor')) if settings.get('curtailment_sensor') else None
                idx = min(range(len(forecast['slots'])), key=lambda i: abs((forecast['slots'][i]-now).total_seconds()))
                observed.append({'time':now.isoformat(),'expected':forecast['solar'][idx]*4,
                    'actual':kw,'curtailed':curb is None or curb.state not in ('off','false','0')})
                self.data['observations'] = [x for x in observed if time_parse(x['time']) >= now-timedelta(days=30)]

    def _import_limit_exceeded(self, additional_kw, cap, rule, prior_phase):
        entity_id = self.data['settings'].get('grid_import_sensor')
        value = numeric_state(self.hass, entity_id)
        state = self.hass.states.get(entity_id) if entity_id else None
        unit = (state.attributes.get('unit_of_measurement') or '').lower() if state else ''
        if value is None or unit not in ('w', 'kw') or cap <= 0:
            return True
        current_kw = value/1000 if unit == 'w' else value
        # Existing draw may include the same already-active rule; avoid double counting.
        incremental = 0 if prior_phase == 'active' else additional_kw
        return current_kw + incremental > cap

    async def evaluate(self, _now=None):
        if self._lock.locked():
            return
        async with self._lock:
            now = dt_util.now()
            try:
                forecast = await fetch_sources(self.hass, self.data['settings'], now)
            except Exception:
                _LOGGER.exception('Forecast retrieval failed')
                return
            ratio = self._correction()
            corrected = [round(kwh*ratio, 5) for kwh in forecast['solar']]
            self.forecast = {**forecast, 'solar_corrected': corrected, 'correction_factor': ratio}
            self._record(forecast, now)
            rows = [{'time':t, 'pv_kwh':corrected[i], 'price':forecast['price'][i]} for i,t in enumerate(forecast['slots'])]
            states = {s.entity_id:s.state for s in self.hass.states.async_all()}
            remaining = sum(x['pv_kwh'] for x in rows if x['time']>=now and x['time'].astimezone(now.tzinfo).date()==now.date())
            reserved_kwh = 0.0
            cap = float(self.data['settings'].get('max_grid_kw',0) or 0)
            active_kw = 0.0
            reserved_windows = []  # (resource, begin, end, planned kW) of accepted higher-priority rules
            for rule in sorted(self.rules, key=lambda r:(-int(r.get('priority',0)),r['id'])):
                state = self.data['states'].setdefault(rule['id'], {})
                old = state.get('phase', 'idle')
                if not rule.get('enabled', True):
                    desired, proposal, reason = 'idle', None, 'Rule disabled'
                elif state.get('override_until') and now < time_parse(state['override_until']):
                    desired, proposal, reason = 'override', None, 'Manual override active'
                else:
                    contexts = {'states':states, 'rows':rows, 'local_tz':now.tzinfo,
                        'solar_remaining':remaining, 'surplus_remaining':max(0,remaining-(forecast['load_kwh'] or 0)-reserved_kwh),
                        'forecast_error_pct':(ratio-1)*100}
                    dependencies, required_entities = self._dependencies(rule.get('conditions'))
                    needs_price = rule.get('objective','min_cost') in ('min_cost','max_spread') or 'price' in dependencies
                    needs_solar = rule.get('objective') == 'most_solar' or 'solar' in dependencies
                    entity_missing = any(states.get(key) in (None,'unknown','unavailable') for key in required_entities)
                    if (needs_price and not any(x is not None for x in forecast['price'])) or (needs_solar and (not forecast['source_entries'] or any(i.startswith('Forecast.Solar') for i in forecast['issues']))) or ('load' in dependencies and forecast['load_kwh'] is None) or entity_missing:
                        proposal = None
                        reason = 'Required forecast unavailable'
                        last_good = time_parse(state['last_good']) if state.get('last_good') else None
                        grace = float(self.data['settings'].get('grace_minutes',30))
                        if last_good and (now-last_good).total_seconds()<grace*60 and state.get('plan'):
                            proposal = state['plan']
                            reason = 'Using last valid plan during grace period'
                        else:
                            reason = 'Safe fallback: missing forecast'
                    else:
                        proposal = plan(rule, rows, contexts)
                        reason = proposal['reason'] if proposal else 'No qualifying slots'
                        state['last_good'] = now.isoformat()
                    locked = state.get('plan')
                    started = time_parse(state['started']) if state.get('started') else None
                    minimum = int(rule.get('min_run_slots',1))*15
                    if old == 'active' and started and (now-started).total_seconds() < minimum*60 and locked:
                        proposal = locked
                        reason = 'Active plan minimum runtime commitment'
                    if proposal:
                        pieces = proposal.get('segments') or [proposal]
                        desired = 'active' if any(time_parse(seg['start'])<=now<time_parse(seg['end']) for seg in pieces) else 'scheduled' if any(now<time_parse(seg['start']) for seg in pieces) else 'idle'
                    else:
                        desired = 'failed' if reason.startswith('Safe fallback') else 'idle'
                    resource = rule.get('resource') or rule['id']
                    if proposal and desired in ('active','scheduled'):
                        pieces = [(time_parse(seg['start']),time_parse(seg['end'])) for seg in proposal.get('segments',[proposal])]
                        kw = float(rule.get('power_kw',0))
                        overlap = [(r,power) for r,a,b,power in reserved_windows if any(x<b and a<y for x,y in pieces)]
                        if any(r == resource for r,power in overlap):
                            desired, reason = 'suppressed', 'Higher-priority rule already reserves resource'
                        elif cap <= 0 and kw > 0:
                            desired, reason = 'suppressed', 'Set a positive global planned power cap first'
                        elif cap and kw + sum(power for _,power in overlap) > cap:
                            desired, reason = 'suppressed', 'Global planned power cap would be exceeded'
                        elif desired == 'active' and self.data['settings'].get('grid_import_sensor') and self._import_limit_exceeded(kw, cap, rule, old):
                            desired, reason = 'suppressed', 'Live grid import limit exceeded or meter unavailable'
                        elif rule.get('min_soc_entity') and (numeric_state(self.hass,rule['min_soc_entity']) is None or
                              numeric_state(self.hass,rule['min_soc_entity']) < float(rule.get('min_soc',0))):
                            desired, reason = 'suppressed', 'Battery SoC safety interlock'
                        elif state.get('last_stopped') and desired == 'active' and (now-time_parse(state['last_stopped'])).total_seconds() < int(rule.get('min_off_slots',0))*900:
                            desired, reason = 'suppressed', 'Minimum off-time not elapsed'
                        else:
                            reserved_windows.extend((resource,x,y,kw) for x,y in pieces)
                            if desired == 'active':
                                active_kw += kw
                            if not self.data['settings'].get('load_includes_planned', False):
                                today = now.date()
                                for segment in proposal.get('segments',[proposal]):
                                    slot=time_parse(segment['start'])
                                    stop=time_parse(segment['end'])
                                    while slot < stop:
                                        if slot >= now and slot.astimezone(now.tzinfo).date() == today:
                                            reserved_kwh += kw * 0.25
                                        slot += STEP
                    if old == 'active' and desired != 'active':
                        state['last_stopped'] = now.isoformat()
                previous_plan = state.get('plan')
                state.update({'phase': desired, 'plan':proposal, 'reason':reason,
                              'evaluated': now.isoformat(), 'source_issues':forecast['issues'][:5]})
                if desired == 'active' and old != 'active':
                    state['started'] = now.isoformat()
                if old != desired:
                    if old == 'active' and desired != 'active':
                        ended = bool(previous_plan and time_parse(previous_plan['end']) <= now)
                        self._publish(rule, 'window_elapsed' if ended else 'stopped', reason=reason, plan=previous_plan)
                    if desired != 'idle' or old != 'active':
                        event = {'active':'started','scheduled':'scheduled','idle':'cancelled',
                                 'suppressed':'suppressed','failed':'failed','override':'override'}[desired]
                        self._publish(rule, event, reason=reason, plan=proposal)
                elif old == 'scheduled' and proposal and state.get('last_event_plan') != proposal:
                    self._publish(rule, 'rescheduled', reason=reason, plan=proposal)
                state['last_event_plan']=proposal
            await self.store.async_save(self.data)
            self._signal()

    def snapshot(self):
        forecast=self.forecast
        return {'rules':self.rules,'states':self.data['states'],'settings':self.data['settings'],
                'forecast':({'times':[t.isoformat() for t in forecast['slots']],
                  'price':forecast['price'], 'solar':forecast['solar_corrected'],
                  'issues':forecast['issues'], 'pv_remaining_kwh':forecast['pv_remaining_kwh'],
                  'load_kwh':forecast['load_kwh'],'correction_factor':forecast['correction_factor']}
                  if forecast else None)}

    async def backtest(self, rule_id, days):
        """Replay issued forecasts and retrieve actual history from HA Recorder.

        Does not pretend historical measured generation was a historical forecast.
        """
        rule=next((r for r in self.rules if r['id']==rule_id), None)
        if not rule:
            raise ValueError('Unknown rule')
        cutoff=dt_util.utcnow()-timedelta(days=min(int(days),30))
        samples=[x for x in self.data['snapshots'] if time_parse(x['issued'])>=cutoff]
        actual_sensor=self.data['settings'].get('pv_actual_sensor')
        _, condition_entities = self._dependencies(rule.get('conditions'))
        recorded_ids = sorted(set(filter(None, [actual_sensor, *condition_entities])))
        actual_states={}
        if recorded_ids:
            try:
                values=await get_instance(self.hass).async_add_executor_job(
                    history.get_significant_states,self.hass,cutoff,dt_util.utcnow(),recorded_ids,
                    None,True,False,False,True)
                actual_states={k:[{'time':v.last_updated.isoformat(),'value':v.state} for v in vs]
                     for k,vs in values.items()}
            except Exception as exc:
                _LOGGER.warning('Recorder backtesting read failed: %s',exc)
        measured=[]
        for point in actual_states.get(actual_sensor, []):
            try:
                measured.append((time_parse(point['time']),float(point['value'])))
            except (ValueError, TypeError):
                continue
        measured.sort()
        measured_times = [t for t,_ in measured]
        actual_entity = self.hass.states.get(actual_sensor) if actual_sensor else None
        unit = (actual_entity.attributes.get('unit_of_measurement') or '').lower() if actual_entity else ''
        divisor = 1000 if unit == 'w' else 1 if unit == 'kw' else None
        conditions_history = {entity:sorted((time_parse(p['time']),p['value']) for p in actual_states.get(entity,[]))
                              for entity in condition_entities}
        simulated=[]
        for snapshot in samples:
            rows=[{'time':time_parse(t),'price':p,'pv_kwh':v} for t,p,v in
                  zip(snapshot['times'], snapshot['prices'],snapshot['solar'])]
            issued=time_parse(snapshot['issued'])
            historic_states={}
            for entity, points in conditions_history.items():
                timestamps=[point[0] for point in points]
                index=bisect_right(timestamps, issued)-1
                if index >= 0:
                    historic_states[entity]=points[index][1]
            context={'states':historic_states,'rows':rows,'local_tz':issued.astimezone().tzinfo,
                     'solar_remaining':sum(snapshot['solar']),'surplus_remaining':max(0,sum(snapshot['solar'])-(snapshot.get('load_kwh') or 0))}
            try:
                result=plan(rule,rows,context)
                if result:
                    # Recorder observations represent actual inverter output, not necessarily
                    # unconstrained sunshine or net surplus.
                    actual_kwh=0.0
                    observed_slots=0
                    if divisor:
                        for segment in result.get('segments', [result]):
                            slot=time_parse(segment['start'])
                            end=time_parse(segment['end'])
                            while slot<end and slot+STEP<=dt_util.utcnow():
                                midpoint=slot+STEP/2
                                idx=bisect_right(measured_times, midpoint)-1
                                if idx>=0 and midpoint-measured_times[idx]<timedelta(hours=1):
                                    actual_kwh+=max(0,measured[idx][1]/divisor)*.25
                                    observed_slots+=1
                                slot+=STEP
                    result['actual_pv_kwh_measured']=round(actual_kwh,3) if observed_slots==result['slots'] else None
                    result['coverage_percent']=round(observed_slots/max(result['slots'],1)*100,1)
                    simulated.append({'issued':snapshot['issued'],**result})
            except (ValueError,TypeError) as err:
                _LOGGER.debug('Skipping invalid historical forecast: %s',err)
        return {'rule_id':rule_id,'days':days,'forecast_snapshots':len(samples),
                'simulations':simulated[-60:],
                'actual_history_points':len(measured), 'condition_history_coverage':{k:len(v) for k,v in conditions_history.items()},
                'confidence':'forecast snapshots available' if samples else 'insufficient historical forecast snapshots',
                'note':'Each result is an independently simulated plan at forecast issuance, not an actual executed action. Actual inverter generation is estimated from Recorder W/kW observations when available. A curtailed inverter measurement is not the same as available solar irradiance. No original forecast is invented for times before snapshot collection began.'}
