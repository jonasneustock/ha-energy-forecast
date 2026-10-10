"""Rule lifecycle sensors; deep schedules stay in the WebSocket API."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    manager=hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RuleSensor(manager, r) for r in manager.rules])


class RuleSensor(SensorEntity):
    _attr_icon='mdi:calendar-clock'
    _attr_should_poll=False

    def __init__(self, manager, rule):
        self.manager=manager
        self.rule_id=rule['id']
        self._attr_name=f"Energy Forecast {rule['name']}"
        self._attr_unique_id=f'{DOMAIN}_{rule["id"]}_status'

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, f'{DOMAIN}_changed', self.async_write_ha_state))

    @property
    def native_value(self):
        return self.manager.data['states'].get(self.rule_id, {}).get('phase','idle')

    @property
    def extra_state_attributes(self):
        d=self.manager.data['states'].get(self.rule_id, {})
        p=d.get('plan') or {}
        return {'rule_id':self.rule_id,'next_start':p.get('start'),'next_end':p.get('end'),
            'expected_cost_eur':p.get('estimated_grid_cost_eur'),'estimated_savings_vs_mean_eur':p.get('estimated_savings_vs_mean_eur'),'expected_solar_kwh':p.get('forecast_pv_kwh'),
            'reason':d.get('reason'),'evaluated':d.get('evaluated'),
            'override_until':d.get('override_until'), 'acknowledged':d.get('acknowledged'),
            'source_issues':d.get('source_issues',[])}
