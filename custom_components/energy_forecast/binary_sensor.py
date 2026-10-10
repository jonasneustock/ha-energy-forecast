"""Binary active state for use in native automation triggers."""
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    m=hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RuleActive(m,r) for r in m.rules])


class RuleActive(BinarySensorEntity):
    _attr_icon='mdi:lightning-bolt'
    _attr_should_poll=False

    def __init__(self,manager,rule):
        self.manager=manager
        self.rule_id=rule['id']
        self._attr_unique_id=f'{DOMAIN}_{rule["id"]}_active'
        self._attr_name=f"Energy Forecast {rule['name']} Active"

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass,f'{DOMAIN}_changed',self.async_write_ha_state))

    @property
    def is_on(self):
        return self.manager.data['states'].get(self.rule_id,{}).get('phase')=='active'
