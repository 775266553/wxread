'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function setup(){
  let nextTimer=0;const timers=new Map(),events=new Map(),windowEvents=new Map(),nodes=new Map();
  function node(selector){if(!nodes.has(selector))nodes.set(selector,{textContent:'',title:'',innerHTML:'',open:false,hidden:false,
    elements:{repository:{value:''},remember:{checked:false},token:{value:''},name:{value:''},minutes:{value:20},email:{value:''},curl:{value:'fixture curl'}},classList:{toggle(){}},
    reset(){},getAttribute(name){return name==='id'?selector.slice(1):null;},
    addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});return nodes.get(selector);}
  const client={connected:false,sessionVersion:1,retryAt:0,cancel:async()=>{},restore:async()=>false,overview:async()=>({})};
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
    assert.equal([...timers.values()][0].ms,60000);scope.pausePolling();assert.equal(timers.size,0);
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
  {
    const {scope,client,node}=setup();await flush();client.connected=true;
    scope.render=()=>{};client.acceptOverview=value=>{client.data=value;};client.overview=async()=>client.data;
    node('#bind-dialog').open=true;vm.runInContext("loginSession='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';bindAccount=null",scope);
    client.state=async()=>({status:'success',binding:{account_id:'third',action:'added',name:'第三个账户'},overview:{revision:3,accounts:[{id:'first'},{id:'second'},{id:'third'}]}});
    await scope.pollQR('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');assert.equal(client.data.accounts.length,3);assert.equal(node('#bind-dialog').open,false);
    assert.equal(node('#toast').textContent,'已添加账户：第三个账户');
  }
  {
    const {scope,client,node}=setup();await flush();client.connected=true;scope.render=()=>{};
    client.acceptOverview=value=>{client.data=value;};client.overview=async()=>client.data;
    node('#bind-dialog').open=true;vm.runInContext("loginSession='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';bindAccount=null",scope);
    client.state=async()=>({status:'success',binding:{account_id:'second',action:'updated',name:'账户二'},overview:{revision:3,accounts:[{id:'first'},{id:'second'}]}});
    await scope.pollQR('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');assert.equal(node('#bind-dialog').open,true,'do not silently close when the same account is scanned');
    assert.equal(node('#qr-status').textContent,'已更新已有账户：账户二，未新增账户');assert.equal(node('#qr-start').disabled,false);
  }
  {
    const {scope,client,node}=setup();await flush();client.connected=true;node('#bind-dialog').open=true;
    vm.runInContext("loginSession='cccccccccccccccccccccccccccccccc'",scope);client.state=async()=>({status:'expired',message:'扫码窗口已结束'});
    await scope.pollQR('cccccccccccccccccccccccccccccccc');assert.equal(node('#qr-start').disabled,false);assert.equal(node('#qr-start').textContent,'重新生成二维码');
    assert.equal(vm.runInContext('loginSession',scope),null,'release an expired QR session before a new binding');
  }
  {
    const {scope,client}=setup();await flush();client.connected=true;scope.render=()=>{};
    let finish;client.overview=()=>new Promise(resolve=>finish=resolve);client.acceptOverview=value=>client.data=value;
    const loading=scope.refresh();scope.bindingSaved({binding:{account_id:'third'},overview:{revision:3,accounts:[{id:'first'},{id:'second'},{id:'third'}]}});
    finish({revision:2,accounts:[{id:'first'},{id:'second'}]});await loading;
    assert.equal(vm.runInContext('data.accounts.length',scope),3,'an older dashboard refresh must not undo a completed binding');
  }
  {
    const {scope,client,node,events}=setup();await flush();client.connected=true;scope.openBind();
    let finish;const cancelled=[];client.start=()=>new Promise(resolve=>finish=resolve);client.cancel=async id=>cancelled.push(id);client.state=async()=>({status:'waiting'});
    const submit=events.get('submit')[0],button=node('#qr-start');
    const pending=submit({preventDefault(){},target:node('#bind-form'),submitter:button});await flush();
    const tab={dataset:{bindTab:'import'}};await events.get('click')[0]({target:{closest:selector=>selector==='[data-bind-tab]'?tab:null}});
    finish('d'.repeat(32));await pending;
    assert.equal(vm.runInContext('loginSession',scope),null,'switching to cURL invalidates a QR dispatch still in flight');
    assert.deepEqual(cancelled,['d'.repeat(32)]);assert.equal(button.disabled,false);
  }
  {
    const {scope,client,node,events}=setup();await flush();client.connected=true;scope.render=()=>{};scope.openBind();
    client.acceptOverview=value=>client.data=value;client.overview=async()=>client.data;
    let finish;client.command=()=>new Promise(resolve=>finish=resolve);vm.runInContext("bindTab='import'",scope);
    const pending=events.get('submit')[0]({preventDefault(){},target:node('#bind-form'),submitter:node('#import-submit')});await flush();
    scope.openBind();finish({status:'success',binding:{account_id:'third',action:'added',name:'第三个账户'},overview:{revision:3,accounts:[{id:'third'}]}});await pending;
    assert.equal(node('#bind-dialog').open,true,'a completed old import must not close a newly opened binding dialog');
    assert.equal(node('#import-submit').disabled,false,'the next binding must have an enabled import button');
    assert.equal(client.data.accounts.length,1,'still show the account that was durably imported');
  }
  {
    const {scope,client,node,events}=setup();await flush();client.connected=true;scope.openBind();
    let finish,calls=0;client.start=()=>{calls++;return new Promise(resolve=>finish=resolve);};client.state=async()=>({status:'waiting'});
    const event={preventDefault(){},target:node('#bind-form'),submitter:node('#qr-start')};
    const pending=events.get('submit')[0](event);await flush();await events.get('submit')[0](event);
    assert.equal(calls,1,'ignore a duplicate submission while dispatch is pending');assert.equal(node('#qr-start').disabled,true);
    finish('e'.repeat(32));await pending;assert.equal(node('#qr-start').disabled,true,'keep the button disabled for an active scan');
  }
  console.log('Pages polling, visibility and login prompt regression checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
