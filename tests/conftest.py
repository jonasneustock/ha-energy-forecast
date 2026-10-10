"""Run pure-planner tests without pulling in Home Assistant as a dependency."""
import importlib.util
import pathlib
import sys
import types

ROOT=pathlib.Path(__file__).parents[1]
for name,path in [('custom_components',ROOT/'custom_components'),
                  ('custom_components.energy_forecast',ROOT/'custom_components'/'energy_forecast')]:
    if name not in sys.modules:
        mod=types.ModuleType(name)
        mod.__path__=[str(path)]
        sys.modules[name]=mod
