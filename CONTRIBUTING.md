# Contributing

1. Open a GitHub issue describing a concrete energy rule or integration defect.
2. Prefer Home Assistant native interfaces and Python's standard library over additional dependencies.
3. Preserve safety checks and add a reproducible test for behavior changes.
4. Run `pytest -q`, `python -m compileall -q custom_components` and `node --check custom_components/energy_forecast/panel.js`.
5. Never commit Tibber credentials, config-entry IDs, user addresses, device tokens, Recorder dumps or private telemetry.
6. Any new condition/operator must define behavior for missing forecasts, timezone boundaries and active plan commitments.

The project aims to avoid speculative abstractions; prefer the smallest correct change.
