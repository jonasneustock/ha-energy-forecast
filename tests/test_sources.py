from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace

from custom_components.energy_forecast.sources import fetch_sources

class FakeServices:
    async def async_call(self,domain,service,data,**kwargs):
        t=datetime(2026,10,10,10,tzinfo=timezone.utc)
        if domain=='tibber':
            return {'prices':{'Home':[{'start_time':(t+i*timedelta(minutes=15)).isoformat(),'price':.2}
                                      for i in range(8)]}}
        return {'wh_period':{(t+timedelta(hours=i)).isoformat():1000 for i in range(3)}}

class FakeHass:
    services=FakeServices()
    config_entries=SimpleNamespace(async_entries=lambda name:[SimpleNamespace(entry_id='pv-a',title='PV-A',data={'azimuth':5},options={}),
                                                           SimpleNamespace(entry_id='pv-b',title='PV-B',data={'azimuth':5},options={})])
    states=SimpleNamespace(get=lambda entity:SimpleNamespace(state='5') if entity=='sensor.load' else None)


def test_source_aggregation_skips_duplicate_config_and_does_not_fill_prices():
    now=datetime(2026,10,10,10,0,tzinfo=timezone.utc)
    x=asyncio.run(fetch_sources(FakeHass(),{'load_sensor':'sensor.load'},now))
    assert x['solar'][:12] == [0.25]*12
    assert x['price'][0]==.2 and x['price'][7]==.2 and x['price'][8] is None
    assert x['load_kwh']==5
    assert 'skipped' in ' '.join(x['issues'])
