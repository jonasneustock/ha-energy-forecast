/* Lightweight HA admin panel: browser controls, no frontend dependencies. */
const D = 'energy_forecast';
const types = [
  ['price','Price (€/kWh)'],['rank_percent','Price percentile (%)'],['price_spread','Future price spread (€/kWh)'],
  ['solar_power','Solar forecast (kW)'],['solar_remaining','Solar remaining today (kWh)'],
  ['surplus_remaining','Surplus remaining today (kWh)'],['solar_before','Solar before deadline (kWh)'],
  ['forecast_error','Solar correction (%)'],['entity','HA entity state/value'],['price_level','Price-level entity equals']
];
const fmtTime = x => x ? new Date(x).toLocaleString() : '—';
const safe = v => String(v ?? '').replace(/[&<>"']/g, x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const options = (values, selected) => values.map(([k,v])=>`<option value="${safe(k)}" ${String(k)===String(selected)?'selected':''}>${safe(v)}</option>`).join('');

class EnergyForecastPanel extends HTMLElement {
  set hass(hass) {
    this._hass=hass;
    if (!this._ready) {this._ready=true; this.start();}
  }
  async start() {
    this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML = `<style>
      :host {display:block;color:var(--primary-text-color);background:var(--primary-background-color);font-family:var(--paper-font-body1_-_font-family, sans-serif);min-height:100vh}
      main{max-width:1120px;margin:auto;padding:24px} header{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
      h1{font-size:1.6rem} h2{font-size:1.15rem} h3{font-size:1rem;margin:0 0 8px}
      section,.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:12px;padding:18px;margin:16px 0}
      .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.grow{flex:1}.column{display:flex;flex-direction:column;gap:6px}
      button{padding:9px 13px;border-radius:7px;border:1px solid var(--divider-color,#ccc);background:var(--secondary-background-color,#eee);color:var(--primary-text-color);cursor:pointer}
      button.primary{background:var(--primary-color,#03a9f4);color:white;border:0}button.danger{color:var(--error-color,#b22)}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--primary-color)}
      input,select{font:inherit;box-sizing:border-box;max-width:100%;padding:7px;border:1px solid var(--divider-color,#aaa);border-radius:6px;color:var(--primary-text-color);background:var(--card-background-color,#fff)}
      label{display:flex;flex-direction:column;gap:5px;font-size:.89rem} .fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
      small,.subdued{color:var(--secondary-text-color,#777)} .rule{border:1px solid var(--divider-color);border-radius:8px;padding:12px;margin:7px 0}
      .group{margin:8px 0 8px 13px;padding:10px;border-left:3px solid var(--primary-color,#03a9f4);background:var(--secondary-background-color,#f8f8f8)}
      .condition{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:7px 0}.condition input{width:145px}.condition select{max-width:220px}
      .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px} .error{color:var(--error-color,#b22)} .good{color:var(--success-color,#287e40)}
      svg{width:100%;height:240px;display:block;background:var(--secondary-background-color,#f7f7f7)}
      .legend{display:flex;gap:18px;align-items:center;font-size:.86rem}.legend i{display:inline-block;width:20px;height:4px;vertical-align:middle;margin-right:6px}
      pre{white-space:pre-wrap;max-height:350px;overflow:auto} [hidden]{display:none!important}
    </style><main><header><div><h1>Energy Forecast Rules</h1><small>48-hour forecast · 15-minute scheduling · 5-minute evaluation</small></div><div class="row"><button id="refresh">Refresh</button><button id="create" class="primary">+ New rule</button></div></header>
      <p id="message" role="status"></p><section><h2>Forecast timeline</h2><div id="summary"></div><svg id="plot" viewBox="0 0 950 240" role="img" aria-label="48-hour electricity price and solar forecast timeline"></svg><div class="legend"><span><i style="background:#f59e0b"></i>PV (kWh/15 min)</span><span><i style="background:#2563eb"></i>Tibber (€/kWh)</span><span><i style="background:#16a34a"></i>Planned active rules</span></div></section>
      <section><h2>Rules & decisions</h2><div id="rules"></div></section>
      <section id="editor" hidden><h2 id="editorTitle">Rule</h2><div id="editorFields" class="fields"></div><h3>Conditions</h3><p><small>All of these conditions are evaluated in each eligible quarter-hour slot. Create nested AND / OR / NOT groups graphically.</small></p><div id="conditions"></div><div class="actions"><button id="save" class="primary">Save rule</button><button id="cancel">Cancel</button></div></section>
      <section><h2>Sources & safety</h2><div id="settings" class="fields"></div><div class="actions"><button id="saveSettings" class="primary">Save settings</button></div></section>
      <section><h2>Historical backtesting</h2><p><small>Replays saved forecasts as originally issued; actual generation comes from HA Recorder when configured. Historical results are unavailable before the integration begins saving snapshots.</small></p><div class="row"><select id="testRule" aria-label="Rule to backtest"></select><select id="days" aria-label="Days of history"><option value="1">1 day</option><option value="7" selected>7 days</option><option value="30">30 days</option></select><button id="backtest">Backtest</button></div><pre id="results"></pre></section>
    </main>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.$('refresh').onclick=()=>this.reload();
    this.$('create').onclick=()=>this.edit(null);
    this.$('cancel').onclick=()=>this.$('editor').hidden=true;
    this.$('save').onclick=()=>this.save();
    this.$('saveSettings').onclick=()=>this.saveSettings();
    this.$('backtest').onclick=()=>this.backtest();
    await this.reload();
  }
  message(text,error=false) {this.$('message').textContent=text; this.$('message').className=error?'error':'good';}
  async call(action,other={}) {return this._hass.callWS({type:`${D}/${action}`,...other});}
  async reload() {
    try {this.data=await this.call('state');this.render();this.message('Updated '+new Date().toLocaleTimeString());}
    catch(e){this.message(e.message||String(e),true);}
  }
  render() {
    const data=this.data, rules=this.$('rules'); rules.replaceChildren();
    for(const rule of data.rules){
      const s=data.states[rule.id]||{},p=s.plan||{};
      const card=document.createElement('div');card.className='rule';
      card.innerHTML=`<div class="row"><strong class="grow">${safe(rule.name)}</strong><span>${safe(s.phase||'idle')}</span><small>Priority ${safe(rule.priority||0)}</small></div>
        <p><small>${safe(s.reason||'Awaiting evaluation')} · ${safe(rule.objective||'min_cost')} · resource ${safe(rule.resource||rule.id)}</small></p>
        <p>Next: ${safe(fmtTime(p.start))} → ${safe(fmtTime(p.end))} · Grid-only cost bound: ${p.estimated_grid_cost_eur==null?'n/a':safe(p.estimated_grid_cost_eur)+' €'} · Indicative saving vs mean price: ${p.estimated_savings_vs_mean_eur==null?'n/a':safe(p.estimated_savings_vs_mean_eur)+' €'}</p>`;
      const actions=document.createElement('div');actions.className='actions';
      const make=(label,click)=>{let b=document.createElement('button');b.textContent=label;b.onclick=click;actions.append(b);};
      make('Edit',()=>this.edit(rule));
      make('Manual override',async()=>{try{await this.call('override',{rule_id:rule.id,minutes:Number(rule.override_minutes??120)});await this.reload();}catch(e){this.message(String(e),true);}});
      make('Resume',async()=>{await this.call('override',{rule_id:rule.id,minutes:0});await this.reload();});
      make('Delete',async()=>{if(!confirm(`Delete ${rule.name}?`))return;try{await this.call('delete',{rule_id:rule.id});await this.reload();}catch(e){this.message(String(e),true);}});
      card.append(actions);rules.append(card);
    }
    if(!data.rules.length) rules.textContent='No rules configured. Create a rule to get started.';
    this.$('testRule').innerHTML=options(data.rules.map(r=>[r.id,r.name]),data.rules[0]?.id);
    this.renderSettings();this.graph();
  }
  graph(){
    const svg=this.$('plot');svg.replaceChildren();const f=this.data.forecast;
    if(!f){this.$('summary').textContent='Waiting for first forecast fetch.';return;}
    this.$('summary').textContent=`Solar remaining today: ${f.pv_remaining_kwh.toFixed(2)} kWh · Load forecast: ${f.load_kwh??'unknown'} kWh · Correction factor: ${f.correction_factor.toFixed(2)} · ${f.issues.join('; ')}`;
    const ns='http://www.w3.org/2000/svg',n=f.times.length;
    const path=(values,max,color)=>{
      let d='';values.forEach((v,i)=>{if(v==null){d+=' ';return;}d+=`${d.endsWith(' ')||!d?'M':'L'} ${Math.round(i*940/Math.max(1,n-1))+5} ${Math.round(230-210*v/max)} `;});
      const p=document.createElementNS(ns,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',color);p.setAttribute('stroke-width','2');svg.append(p);
    };
    const pv=f.solar||[],prices=f.price||[];
    const valid=prices.filter(x=>x!=null),low=Math.min(0,...valid),max=Math.max(.01,...valid)-low;
    path(pv,Math.max(.01,...pv),'#f59e0b');path(prices.map(x=>x==null?null:x-low),max,'#2563eb');
    for(const s of Object.values(this.data.states||{})) if(s.plan && ['active','scheduled'].includes(s.phase)){
      const first=new Date(f.times[0]).getTime(),end=new Date(f.times[n-1]).getTime()+900000;
      for(const segment of s.plan.segments||[s.plan]){const l=new Date(segment.start).getTime(),r=new Date(segment.end).getTime();
      const x=5+Math.max(0,Math.min(1,(l-first)/(end-first)))*940,w=Math.max(0,(Math.min(r,end)-Math.max(l,first))/(end-first)*940);
      if(w>0){const rect=document.createElementNS(ns,'rect');rect.setAttribute('x',x);rect.setAttribute('y','236');rect.setAttribute('width',w);rect.setAttribute('height','4');rect.setAttribute('fill','#16a34a');svg.append(rect);}}
    }
  }
  edit(rule){
    this.draft=rule?structuredClone(rule):{name:'New energy rule',enabled:true,objective:'min_cost',duration_slots:4,min_run_slots:1,priority:0,power_kw:1,energy_kwh:0,resource:'',conditions:{op:'and',children:[]}};
    this.$('editorTitle').textContent=rule?'Edit rule':'New rule';
    const def=[['name','Name','text'],['resource','Resource/device key','text'],['priority','Priority','number'],['power_kw','Power (kW)','number'],['energy_kwh','Required energy (kWh; optional)','number'],['duration_slots','Duration (15-minute slots)','number'],['min_run_slots','Minimum runtime (15-minute slots)','number'],['max_run_slots','Maximum runtime (15-minute slots; default 192)','number'],['min_off_slots','Minimum off time (15-minute slots)','number'],['allowed_start_hour','Allowed start time (HH:MM, optional)','time'],['allowed_end_hour','Allowed end time (HH:MM, optional)','time'],['earliest','Earliest start (optional)','datetime-local'],['deadline','Deadline (optional)','datetime-local'],['manual_entity_id','Device entity for manual UI override detection (optional)','text'],['override_minutes','Override duration (minutes, default 120)','number'],['min_soc_entity','Min SoC sensor (optional)','text'],['min_soc','Minimum SoC (%)','number']];
    const fields=this.$('editorFields');fields.replaceChildren();
    for(const [key,label,type] of def){const wrap=document.createElement('label');wrap.textContent=label;const inp=document.createElement('input');inp.type=type;inp.value=type==='datetime-local'&&this.draft[key]?this.localValue(this.draft[key]):this.draft[key]??'';
      if(type==='number')inp.step='any';inp.onchange=()=>{this.draft[key]=type==='number'?Number(inp.value):type==='datetime-local'?(inp.value?new Date(inp.value).toISOString():''):inp.value;};wrap.append(inp);fields.append(wrap);}
    const wrap=document.createElement('label');wrap.textContent='Objective';const select=document.createElement('select');select.innerHTML=options([['min_cost','Minimum grid energy cost'],['most_solar','Maximum solar generation'],['earliest','Earliest qualified window'],['max_spread','Maximum future price spread']],this.draft.objective);select.onchange=()=>this.draft.objective=select.value;wrap.append(select);fields.append(wrap);
    const interruptible=document.createElement('label');interruptible.textContent='Allow pausing between minimum-runtime chunks';const split=document.createElement('input');split.type='checkbox';split.checked=!!this.draft.interruptible;split.onchange=()=>this.draft.interruptible=split.checked;interruptible.append(split);fields.append(interruptible);
    const enabled=document.createElement('label');enabled.textContent='Rule enabled';const cb=document.createElement('input');cb.type='checkbox';cb.checked=this.draft.enabled!==false;cb.onchange=()=>this.draft.enabled=cb.checked;enabled.append(cb);fields.append(enabled);
    this.draft.conditions ||= {op:'and',children:[]};this.renderConditions();this.$('editor').hidden=false;this.$('editor').scrollIntoView({behavior:'smooth'});
  }
  localValue(s){const d=new Date(s);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
  renderConditions(){const root=this.$('conditions');root.replaceChildren();root.append(this.conditionElement(this.draft.conditions,null));}
  conditionElement(node,parent){
    const box=document.createElement('div');box.className=node.op==='condition'?'condition':'group';
    const select=document.createElement('select');
    select.innerHTML=options([['and','AND group'],['or','OR group'],['not','NOT group'],['condition','Condition']],node.op||'condition');
    select.onchange=()=>{node.op=select.value;if(node.op!=='condition')node.children=[];else{delete node.children;node.type='price';node.value=0.2;node.operator='<=';}this.renderConditions();};box.append(select);
    if(parent){const rm=document.createElement('button');rm.type='button';rm.textContent='Remove';rm.onclick=()=>{parent.children.splice(parent.children.indexOf(node),1);this.renderConditions();};box.append(rm);}
    if(node.op==='condition'){
      const type=document.createElement('select');type.innerHTML=options(types,node.type);type.onchange=()=>{node.type=type.value;this.renderConditions();};box.append(type);
      if(['entity','price_level'].includes(node.type)){let e=document.createElement('input');e.placeholder='sensor.entity_id';e.setAttribute('aria-label','HA entity ID');e.value=node.entity_id||'';e.onchange=()=>node.entity_id=e.value;box.append(e);}
      const compare=document.createElement('select');compare.innerHTML=options(['<','<=','>','>=','==','!='].map(x=>[x,x]),node.operator||'>=');compare.onchange=()=>node.operator=compare.value;box.append(compare);
      const value=document.createElement('input');value.placeholder='Value';value.setAttribute('aria-label','Threshold or state value');value.value=node.value??'';value.onchange=()=>node.value=value.value;box.append(value);
      if(node.type==='solar_before'){const deadline=document.createElement('input');deadline.type='time';deadline.value=node.deadline||'18:00';deadline.setAttribute('aria-label','Solar deadline');deadline.onchange=()=>node.deadline=deadline.value;box.append(deadline);}
    }else{
      const kids=document.createElement('div');for(const child of node.children||[]) kids.append(this.conditionElement(child,node));box.append(kids);
      if(node.op!=='not'||node.children.length<1){const add=document.createElement('button');add.textContent='+ Condition';add.onclick=()=>{node.children.push({op:'condition',type:'price',value:0.2,operator:'<='});this.renderConditions();};box.append(add);const group=document.createElement('button');group.textContent='+ Group';group.onclick=()=>{node.children.push({op:'and',children:[]});this.renderConditions();};box.append(group);}
    }
    return box;
  }
  async save(){try{await this.call('save',{rule:this.draft});this.$('editor').hidden=true;await this.reload();}
    catch(e){this.message(e.message||String(e),true);}}
  renderSettings(){const f=this.data.settings,root=this.$('settings');root.replaceChildren();
    const defs=[['load_sensor','Remaining-day load forecast sensor','text'],['pv_actual_sensor','Measured PV output (W or kW)','text'],['curtailment_sensor','Binary sensor: curtailment active','text'],['tibber_home','Tibber home name (if multiple)','text'],['max_grid_kw','Global maximum grid import (kW; required)','number'],['grid_import_sensor','Measured grid import power sensor (W or kW)','text'],['grace_minutes','Missing data grace period (minutes)','number']];
    this.settingsDraft=structuredClone(f);
    for(const [key,label,type] of defs){let wrap=document.createElement('label');wrap.textContent=label;let input=document.createElement('input');input.type=type;input.value=f[key]??'';input.onchange=()=>this.settingsDraft[key]=type==='number'?Number(input.value):input.value;wrap.append(input);root.append(wrap);}
    let label=document.createElement('label');label.textContent='Load forecast already includes planned loads';let check=document.createElement('input');check.type='checkbox';check.checked=!!f.load_includes_planned;check.onchange=()=>this.settingsDraft.load_includes_planned=check.checked;label.append(check);root.append(label);
  }
  async saveSettings(){try{await this.call('settings',{settings:this.settingsDraft});await this.reload();}catch(e){this.message(String(e),true);}}
  async backtest(){const id=this.$('testRule').value;if(!id)return this.message('Create a rule first',true);
    this.$('results').textContent='Reading available historical forecasts and Recorder data…';
    try{const v=await this.call('backtest',{rule_id:id,days:Number(this.$('days').value)});
      const summary={forecast_snapshots:v.forecast_snapshots,confidence:v.confidence,simulations:v.simulations,
        actual_history_points:v.actual_history_points,note:v.note};
      this.$('results').textContent=JSON.stringify(summary,null,2);
    }catch(e){this.$('results').textContent=String(e);}}
}
customElements.define('energy-forecast-panel',EnergyForecastPanel);
