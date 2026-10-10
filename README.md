# Energy Forecast Rules for Home Assistant

**Energy-only, forecast-driven rule scheduling** using native Tibber and Forecast.Solar integrations. Home Assistant's own automations perform device actions; this integration computes schedules, publishes lifecycle events and exposes diagnostic entities. Rules are created with a graphical, admin-only editor.

> **Status: 0.1.0 pre-release.** Planner unit tests and JavaScript syntax checks pass. It has **not yet been exercised against a live Home Assistant 2026.x installation**. Start with harmless test switches; do not connect uncontrolled battery, heating or high-power loads without verification.

## Features

- 48-hour planning horizon with fixed 15-minute slots; recompute every five minutes.
- Built-in `tibber.get_prices` and `forecast_solar.get_forecast`, no additional credentials.
- Automatically discover separate Forecast.Solar configuration entries; combine their energy periods, avoiding duplicate configurations.
- Native price and PV conditions: comparison thresholds, price percentiles, spread to a future higher price, predicted PV, energy before a deadline, remaining-day solar surplus, historical correction, and arbitrary HA entity states.
- Graphical nested AND/OR/NOT groups; no YAML configuration required for rules.
- Lowest grid-price, maximum solar, earliest permitted and future-price-spread scheduling; optional interruptible chunks, deadlines, time-of-day constraints and minimum runtimes/off times.
- Independent optimizers; priority arbitration prevents overlapping resource ownership, and global power and per-rule minimum SoC constraints suppress unsafe plans.
- Manual overrides, missing-data grace period, persisted plans, decision diagnostics and an event contract for external automations.
- Hourly issued-forecast snapshots retained for 30 days, Recorder-based actual PV comparisons and optional automatic PV correction on days without measured curtailment.
- Graphical 48-hour forecast/schedule timeline and backtest interface.

## Install locally / via HACS

1. Install and configure Home Assistant's **Tibber** and **Forecast.Solar** integrations. Install a load forecast sensor if desired.
2. Copy `custom_components/energy_forecast` to `<config>/custom_components/energy_forecast`, then restart Home Assistant. When published as a public GitHub repository, add that repository in HACS as a **Custom repository → Integration** instead.
3. Go to **Settings → Devices & services → Add Integration**, find **Energy Forecast Rules**, and add it.
4. Open **Energy Forecast** in the Home Assistant sidebar. Configure source sensors and **set the global grid-import/power limit** before enabling powered rules. A zero/unset cap fails closed.
5. Create a rule in the graphical editor. Assign one `resource` key to mutually exclusive actions controlling the same device (e.g. `battery_main`). Higher numerical priority wins.
6. Use native HA automations to act on `binary_sensor.energy_forecast_..._active` or `energy_forecast_rule` events. Build and verify appropriate device-side power/temperature/current protections in those automations.

## Example automation

Create this in Home Assistant's **graphical automation editor** (the YAML representation below is for documentation only). Find the rule identifier from the event payload or the rule sensor's `rule_id` attribute. **Adapt** the scripts below to your actual equipment and fail-safe logic.

```yaml
alias: Forecast-driven appliance
triggers:
  - trigger: event
    event_type: energy_forecast_rule
    event_data:
      rule_id: REPLACE_WITH_YOUR_RULE_ID
      phase: started
    id: start
  - trigger: event
    event_type: energy_forecast_rule
    event_data:
      rule_id: REPLACE_WITH_YOUR_RULE_ID
      phase: stopped
    id: stop
  - trigger: event
    event_type: energy_forecast_rule
    event_data:
      rule_id: REPLACE_WITH_YOUR_RULE_ID
      phase: window_elapsed
    id: finish
  - trigger: event
    event_type: energy_forecast_rule
    event_data:
      rule_id: REPLACE_WITH_YOUR_RULE_ID
      phase: suppressed
    id: safety
  - trigger: event
    event_type: energy_forecast_rule
    event_data:
      rule_id: REPLACE_WITH_YOUR_RULE_ID
      phase: failed
    id: failure
conditions: []
actions:
  - choose:
      - conditions: "{{ trigger.id == 'start' }}"
        sequence:
          - action: switch.turn_on
            target:
              entity_id: switch.my_test_appliance
          - action: energy_forecast.acknowledge
            data:
              rule_id: REPLACE_WITH_YOUR_RULE_ID
              status: started
    default:
      - action: switch.turn_off
        target:
          entity_id: switch.my_test_appliance
mode: queued
```

This deliberately uses a **test switch**. `energy_forecast.acknowledge` records external action feedback; emitting a `started` event does **not** prove a device really started. The example should acknowledge only after the action succeeds. For safety-critical equipment, verify the resulting sensor state before acknowledging.

## Rule details

- Conditions are evaluated for **every** candidate 15-minute slot in a contiguous run. A NOT group negates exactly one child.
- Numeric values are in **€/kWh, kW, kWh or percent** as indicated. HA entity equality may use string values such as `on`; unknown/unavailable states fail closed.
- The cost metric is a **grid-only upper-bound estimate**. The selected remaining-day consumption sensor has no hour-by-hour shape; subtracting gross PV power directly from 15-minute household grid demand would imply false accuracy. `surplus_remaining` is a **remaining-day aggregate** (`PV - forecast household consumption - accepted new scheduled loads`). If your load prediction already contains future controlled loads, enable the corresponding checkbox to avoid double counting.
- The planning window is 48 hours **but Tibber may publish less than 48 hours**. Slots lacking prices are never filled with guessed values; price-dependent plans only use published intervals.
- `min_run_slots` locks started actions against ordinary rescheduling for that duration. Safety constraints and priorities can still suppress them. `min_off_slots` prevents a stopped rule reactivating immediately. If `interruptible` is checked, non-contiguous minimum-run chunks can be selected.
- When rule schedules change, external automations receive an event. Reconciliation itself runs every five minutes: do not treat it as a sub-second safety protection.
- HA timezone-aware forecast instants are normalized to UTC; local daily budget and operating hours are displayed/evaluated in HA local time, including DST transitions.

## Solar correction and backtesting

For adaptive solar correction, provide a **measured PV output sensor** in W or kW plus a **curtailment binary sensor** (`on` means curtailed). At least 16 non-curtailed samples are required before applying a robust median ratio, capped between 0.65× and 1.20×; otherwise correction remains 1.0. This prevents a zero-export inverter from being treated as cloudy when it merely clips surplus generation. The sensor is sampled at five-minute evaluation intervals.

The integration retains hourly snapshots of forecasts **as originally issued** and reads actual PV observations from Home Assistant Recorder when you select Backtest. It compares simulated future plans per issued snapshot with actual measured solar when sufficient historical measurements exist. It cannot reconstruct historical forecasts from Recorder states if no original snapshots were recorded, and does **not** claim its hypothetical schedules were physically executed. Backtesting is limited to the past 30 days.

## Safety and limitations

- **External actuation is intentionally separate.** The integration cannot directly enforce a downstream load's hardware limits. The global configured cap accounts for planned rule loads; an optional measured grid-import sensor additionally checks live import, but an unexpected load can still exceed the limit before the next five-minute evaluation. Configure proper fuse/device limits and fast native protections.
- Rule conflict priorities prevent overlapping **plans** for the same resource, not global optimality of all loads. Independent optimization was a deliberate architectural choice.
- Forecast.Solar discovery is per configuration entry. Multiple entries with identical config are skipped; distinct installations must be represented correctly. Duplicate physical installations with different configurations cannot be identified automatically.
- User-defined entity conditions rely on **current** HA states, not predictions of their future state; historical replay may not reproduce those state conditions unless persisted separately.
- Historical PV comparisons do not establish household *net* surplus from a remaining-day aggregate consumption forecast. Price-only optimization is supported without a solar forecast; solar-dependent conditions fail closed on source errors.
- Use the built-in configuration storage APIs; no external database, broker, local webserver, API keys or third-party Python packages are required.

## Development

```bash
python -m pip install pytest
python -m pytest -q
python -m compileall -q custom_components
node --check custom_components/energy_forecast/panel.js
```

Public-repository CI includes Pytest, HACS validation and Hassfest. The official docs used for this integration are [Tibber get_prices](https://www.home-assistant.io/actions/tibber.get_prices/), [Forecast.Solar get_forecast](https://www.home-assistant.io/actions/forecast_solar.get_forecast/), [HA integration config flows](https://developers.home-assistant.io/docs/core/integration/config_flow/), [HA custom panels](https://developers.home-assistant.io/docs/frontend/custom-ui/creating-custom-panels/) and [HACS integration requirements](https://www.hacs.dev/docs/publish/integration/).

## Publishing

The intended public GitHub repository is `jonasneustock/ha-energy-forecast`. Its GitHub repository must be created before linking a HACS custom repository, and release tags should be created after CI validation. This source archive is not a published repository, nor proof of compatibility with an actual running Home Assistant instance.

Licensed under MIT.
