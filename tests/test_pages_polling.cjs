'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function setup(){
  let nextTimer=0;const timers=new Map(),events=new Map(),windowEvents=new Map(),nodes=new Map();
  function node(selector){if(!nodes.has(selector))nodes.set(selector,{textContent:'',title:'',innerHTML:'',open:false,hidden:false,
    elements:{repository:{value:''},remember:{checked:false},token:{value:''}},classList:{toggle(){}},
    addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});return nodes.get(selector);}
  const client={connected:false,sessionVersion:1,retryAt:0,restore:async()=>false,overview:async()=>({})};
  const document={querySelector:node,querySelectorAll:()=>[],visibilityState:'visible',addEventListener:(name,fn)=>{if(!events.has(name))events.set(name,[]);events.get(name).push(fn);}};
  const scope=vm.createContext({window:{githubClient:client,scrollTo(){},addEventListener:(name,fn)=>windowEvents.set(name,fn)},document,
    sodium:{ready:Promise.resolve()},location:{hostname:'localhost',pathname:'/'},localStorage:{getItem:()=>null},
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../pages/static/app.js'),'utf8'),scope);
  vm.runInContext("view='settings'",scope);
  return {scope,client,timers,events,windowEvents,node,fire:async()=>{const [id,timer]=[...timers.entries()][0];timers.delete(id);return timer.fn();}};
}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
async function main(){
  {
    const {scope,client,timers,fire,node}=setup();await flush();client.connected=true;let calls=0,finish;
    client.overview=()=>{calls++;return new Promise(resolve=>finish=resolve);};
    const manual=scope.refresh();scope.startPolling(true);const tick=fire();const second=scope.refresh();
    assert.equal(calls,1,'manual refresh and polling share one request');assert.equal(timers.size,0,'do not schedule another poll before the current one finishes');
    finish({accounts:[]});await Promise.all([manual,tick,second]);assert.equal(node('#connection').textContent,'GitHub 已连接');assert.equal(timers.size,1);
    assert.equal([...timers.values()][0].ms,10000);scope.pausePolling();assert.equal(timers.size,0);
  }
  {
    const {scope,client,node,timers,fire}=setup();await flush();client.connected=true;
    client.overview=async()=>{throw Object.assign(new Error('offline'),{code:'NETWORK',retryable:true});};scope.startPolling(true);await fire();
    assert.equal(node('#auth-dialog').open,true);node('#auth-dialog').open=false;
    await scope.refresh();assert.equal(node('#auth-dialog').open,false,'an outage does not prompt for login');assert.equal(node('#connection').textContent,'网络波动 · 自动重试');
    assert.equal([...timers.values()][0].ms,30000,'back off after an outage');client.overview=async()=>({accounts:[]});await fire();assert.equal(node('#connection').textContent,'GitHub 已连接');
  }
  {
    const {scope,client,timers,events,windowEvents,fire,node}=setup();await flush();client.connected=true;node('#auth-dialog').open=false;client.overview=async()=>({accounts:[]});scope.startPolling();
    scope.document.visibilityState='hidden';events.get('visibilitychange')[0]();assert.equal(timers.size,0,'pause hidden dashboard polling');
    scope.document.visibilityState='visible';events.get('visibilitychange')[0]();assert.equal([...timers.values()][0].ms,0,'refresh immediately when returning');await fire();
    windowEvents.get('online')();assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,0,'resume as soon as the network returns');
  }
  {
    const {scope,client,node}=setup();await flush();client.connected=true;let finish;client.overview=()=>new Promise(resolve=>finish=resolve);
    const old=scope.refresh();client.sessionVersion++;client.connected=false;node('#connection').textContent='尚未连接';finish({accounts:[]});await old;
    assert.equal(node('#connection').textContent,'尚未连接','a stale result cannot show a logged-out page as connected');
  }
  {
    const {scope,client,node}=setup();await flush();client.connected=true;node('#auth-dialog').open=false;
    client.overview=async()=>{client.connected=false;client.sessionVersion++;throw Object.assign(new Error('Token invalid'),{code:'AUTHENTICATION'});};
    await scope.refresh();assert.equal(node('#auth-dialog').open,true);assert.equal(node('#auth-error').textContent,'Token invalid');assert.equal(node('#connection').textContent,'需要重新连接');
  }
  console.log('Pages polling, visibility and login prompt regression checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
