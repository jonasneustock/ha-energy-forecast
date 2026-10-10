"""Energy Forecast Rules: HA-native forecast, rules and schedule publication."""
from pathlib import Path
import logging

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from .backend import Manager
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ['sensor', 'binary_sensor']


async def async_setup(hass, config):
    """Register an admin-only panel and authenticated WebSocket API once."""
    hass.data.setdefault(DOMAIN, {})
    await hass.http.async_register_static_paths([
        StaticPathConfig(f'/{DOMAIN}/panel.js', str(Path(__file__).parent / 'panel.js'), False)
    ])
    await panel_custom.async_register_panel(
        hass, webcomponent_name='energy-forecast-panel',
        frontend_url_path='energy-forecast',
        module_url=f'/{DOMAIN}/panel.js',
        sidebar_title='Energy Forecast',sidebar_icon='mdi:solar-power',
        require_admin=True, config_panel_domain=DOMAIN,
    )
    async def service_ack(call):
        await get_manager(hass).acknowledge(call.data['rule_id'],call.data['status'])
    hass.services.async_register(DOMAIN, 'acknowledge', service_ack, schema=vol.Schema({
        vol.Required('rule_id'): cv.string,
        vol.Required('status'): vol.In(('started','completed','failed','stopped'))
    }))
    async def service_override(call):
        await get_manager(hass).override(call.data['rule_id'], call.data['minutes'])
    hass.services.async_register(DOMAIN, 'override', service_override, schema=vol.Schema({
        vol.Required('rule_id'): cv.string,
        vol.Required('minutes'): vol.All(vol.Coerce(int), vol.Range(min=0,max=10080))
    }))
    for command in (ws_state, ws_save, ws_delete, ws_settings, ws_override, ws_ack, ws_backtest):
        websocket_api.async_register_command(hass, command)
    return True


async def async_setup_entry(hass, entry):
    manager = Manager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = manager
    await manager.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        manager = hass.data[DOMAIN].pop(entry.entry_id, None)
        if manager:
            await manager.stop()
    return ok


def get_manager(hass):
    active = hass.data.get(DOMAIN, {})
    if not active:
        raise ValueError('Energy Forecast is not configured')
    return next(iter(active.values()))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/state'})
@websocket_api.require_admin
@callback
def ws_state(hass, connection, msg):
    try:
        connection.send_result(msg['id'], get_manager(hass).snapshot())
    except ValueError as err:
        connection.send_error(msg['id'], 'not_configured', str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/save', vol.Required('rule'):dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(hass, connection, msg):
    try:
        rule_id = await get_manager(hass).save_rule(msg['rule'])
        connection.send_result(msg['id'], {'id':rule_id})
    except (ValueError, TypeError, KeyError) as err:
        connection.send_error(msg['id'], 'invalid_rule', str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/delete',vol.Required('rule_id'):str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete(hass, connection, msg):
    try:
        await get_manager(hass).remove_rule(msg['rule_id'])
        connection.send_result(msg['id'])
    except ValueError as err:
        connection.send_error(msg['id'],'invalid_rule',str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/settings',vol.Required('settings'):dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings(hass, connection, msg):
    try:
        await get_manager(hass).update_settings(msg['settings'])
        connection.send_result(msg['id'])
    except (ValueError, TypeError) as err:
        connection.send_error(msg['id'],'invalid_settings',str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/override',vol.Required('rule_id'):str,vol.Required('minutes'):int})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_override(hass, connection, msg):
    try:
        await get_manager(hass).override(msg['rule_id'],msg['minutes'])
        connection.send_result(msg['id'])
    except ValueError as err:
        connection.send_error(msg['id'],'invalid_override',str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/ack',vol.Required('rule_id'):str,vol.Required('status'):str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_ack(hass, connection, msg):
    try:
        await get_manager(hass).acknowledge(msg['rule_id'],msg['status'])
        connection.send_result(msg['id'])
    except ValueError as err:
        connection.send_error(msg['id'],'invalid_ack',str(err))


@websocket_api.websocket_command({vol.Required('type'):f'{DOMAIN}/backtest',vol.Required('rule_id'):str,vol.Optional('days',default=7):vol.All(int,vol.Range(min=1,max=30))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_backtest(hass, connection, msg):
    try:
        connection.send_result(msg['id'],await get_manager(hass).backtest(msg['rule_id'],msg['days']))
    except ValueError as err:
        connection.send_error(msg['id'],'invalid_backtest',str(err))
