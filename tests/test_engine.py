from datetime import datetime, timedelta, timezone
import pytest

from custom_components.energy_forecast.engine import (
    grid, spread_periods, normalize_prices, matches, plan, validate_rule, validate_condition
)


def make_rows(n=192, price=.1):
    slots=grid(datetime(2026,10,10,12,5,tzinfo=timezone.utc))
    return [{'time':t,'price':price,'pv_kwh':0.1} for t in slots[:n]]


def context(rows):
    return {'states':{'sensor.soc':'75','binary_sensor.on':'on'},'rows':rows,'local_tz':timezone.utc,
            'solar_remaining':5,'surplus_remaining':3}


def test_grid_and_dst_in_utc():
    x=grid(datetime(2026,10,25,1,7,tzinfo=timezone.utc))
    assert len(x)==192 and x[0].minute==0
    assert all((b-a).total_seconds()==900 for a,b in zip(x,x[1:]))


def test_energy_resampling_preserves_kwh():
    t=datetime(2026,10,10,12,0,tzinfo=timezone.utc)
    slots=[t+timedelta(minutes=i*15) for i in range(4)]
    assert spread_periods({t.isoformat():1000},slots)==[250]*4


def test_no_invented_prices():
    t=datetime(2026,10,10,12,tzinfo=timezone.utc)
    slots=[t+timedelta(minutes=15*i) for i in range(8)]
    x=normalize_prices([{'start_time':t.isoformat(),'price':.2}],slots)
    assert x[:1]==[.2] and x[1:]==[None]*7


def test_nested_conditions():
    r=make_rows()
    n={'op':'and','children':[{'type':'price','value':.2,'operator':'<'},
        {'op':'not','children':[{'type':'entity','entity_id':'sensor.soc','value':90,'operator':'>='}]}]}
    validate_condition(n)
    assert matches(n,r[0],context(r))
    assert not matches({'type':'entity','entity_id':'sensor.soc','operator':'>','value':90},r[0],context(r))


def test_planner_chooses_cheapest_consecutive_window():
    r=make_rows(12, .5)
    for i in (4,5,6,7):r[i]['price']=.1
    rule={'name':'Charge','duration_slots':4,'min_run_slots':1,'objective':'min_cost','power_kw':2}
    p=plan(rule,r,context(r))
    assert p['start']==r[4]['time'].isoformat()
    assert p['energy_kwh']==2


def test_energy_requirement_length_and_deadline():
    r=make_rows(12)
    rule={'name':'EV','duration_slots':1,'energy_kwh':3,'power_kw':2,'deadline':r[5]['time'].isoformat()}
    assert plan(rule,r,context(r)) is None
    del rule['deadline']
    assert plan(rule,r,context(r))['slots']==6


def test_invalid_conditions_rejected():
    with pytest.raises(ValueError):validate_condition({'type':'__code__','value':1})
    with pytest.raises(ValueError):validate_condition({'op':'not','children':[]})
    with pytest.raises(ValueError):validate_rule({'name':'x','duration_slots':5,'min_run_slots':6})


def test_interruptible_rule_avoids_costly_gap():
    r=make_rows(12,.5)
    for i in (1,2,7,8):r[i]['price']=.1
    rule={'name':'Flexible load','duration_slots':4,'min_run_slots':2,'min_off_slots':1,
          'objective':'min_cost','power_kw':1,'interruptible':True}
    p=plan(rule,r,context(r))
    assert p['slots']==4
    assert len(p['segments'])==2
    assert p['segments'][0]['start']==r[1]['time'].isoformat()
    assert p['segments'][1]['start']==r[7]['time'].isoformat()
