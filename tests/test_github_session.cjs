'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../pages/static/github.js'),'utf8');
const storage=()=>{const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key),values};};
const response=(status,value={},headers={})=>({status,ok:status>=200 && status<300,headers:{get:name=>headers[name]??null},text:async()=>JSON.stringify(value)});
function setup(){
  let now=100000;
  class Clock extends Date{static now(){return now;}}
  const sessionStorage=storage(),localStorage=storage();
  const sodium={ready:Promise.resolve(),base64_variants:{ORIGINAL:1},
    to_base64:bytes=>Buffer.from(bytes).toString('base64'),from_base64:text=>new Uint8Array(Buffer.from(text,'base64')),
    from_string:text=>new TextEncoder().encode(text),to_string:bytes=>new TextDecoder().decode(bytes),
    crypto_box_keypair:()=>({publicKey:new Uint8Array(32).fill(1),privateKey:new Uint8Array(32).fill(2)}),memzero:bytes=>bytes.fill(0)};
  const scope=vm.createContext({window:{},Date:Clock,sessionStorage,localStorage,sodium,crypto:require('node:crypto').webcrypto,AbortController,setTimeout,clearTimeout,structuredClone:value=>JSON.parse(JSON.stringify(value))});
  vm.runInContext(source,scope);const client=scope.window.githubClient;
  client.delay=async ms=>{now+=ms;};
  const connect=()=>{client.repository='test/repo';client.token='fixture-token';client.branch='main';client.clientId='a'.repeat(32);client.pair=sodium.crypto_box_keypair();client.serverKey=client.b64(new Uint8Array(32).fill(3));client.registeredAt=now;};
  connect();return {scope,client,sessionStorage,localStorage,connect,advance:ms=>now+=ms,now:()=>now};
}
async function main(){
  {
    const {scope,client}=setup();let calls=0;scope.fetch=async()=>{calls++;if(calls<3)throw new TypeError('offline');return response(200,{ok:true});};
    assert.equal((await client.request('')).ok,true);assert.equal(calls,3);assert.equal(client.connected,true);
    calls=0;scope.fetch=async()=>{calls++;return response(503);};await assert.rejects(client.request(''),e=>e.code==='NETWORK');assert.equal(calls,3);assert.equal(client.connected,true);
    calls=0;scope.fetch=async()=>{calls++;throw new TypeError('offline');};await assert.rejects(client.request('/dispatch','POST',{}),e=>e.code==='NETWORK');assert.equal(calls,1,'never repeat a mutation after an ambiguous failure');
  }
  {
    const {scope,client,sessionStorage,now}=setup();client.remember=true;client.sessionExpires=now()+3600000;assert.equal(client.saveSession(),true);
    const privateKey=client.pair.privateKey;scope.fetch=async()=>response(401);await assert.rejects(client.request(''),e=>e.code==='AUTHENTICATION');
    assert.equal(client.connected,false);assert.equal(sessionStorage.values.size,0);assert.ok(privateKey.every(byte=>byte===0),'zero private key on logout');
  }
  for(const status of [401,200]){
    const {scope,client,connect}=setup();let finish;scope.fetch=()=>new Promise(resolve=>finish=resolve);
    const old=client.request('');await Promise.resolve();client.disconnect();connect();client.token='new-fixture-token';finish(response(status,{old:true}));
    await assert.rejects(old,e=>e.code==='SESSION_CHANGED');assert.equal(client.connected,true,'a stale response cannot destroy the newer login');assert.equal(client.token,'new-fixture-token');
  }
  {
    const {scope,client,now}=setup();let calls=0;scope.fetch=async()=>{calls++;return response(403,{message:'API rate limit exceeded'},{'x-ratelimit-remaining':'0','x-ratelimit-reset':String((now()+300000)/1000)});};
    await assert.rejects(client.request(''),e=>e.code==='RATE_LIMIT');assert.equal(client.retryAt,now()+300000);
    await assert.rejects(client.request(''),e=>e.code==='RATE_LIMIT');assert.equal(calls,1,'honor rate-limit reset before making another request');assert.equal(client.connected,true);
  }
  {
    const {scope,client}=setup();scope.fetch=async()=>response(403,{message:'Resource not accessible'});
    await assert.rejects(client.request(''),e=>e.code==='PERMISSION');assert.equal(client.connected,true,'permission errors do not erase credentials');
  }
  {
    const {scope,client,sessionStorage,localStorage,now}=setup();
    scope.fetch=async()=>response(200,{default_branch:'main'});
    client.file=async()=>({value:{public_key:client.serverKey}});client.command=async()=>({overview:{accounts:[]}});
    await client.connect('test/repo','fixture-token');assert.equal(sessionStorage.values.size,0,'memory-only is the default');
    const result=await client.connect('test/repo','fixture-token',()=>{},true);assert.equal(result.saved,true);
    assert.equal(localStorage.values.get('wxread-repository'),'test/repo');assert.equal(localStorage.values.size,2,'browser credentials are saved only after opt-in');
    const saved=JSON.parse(localStorage.getItem('wxread-session-v1'));assert.equal(saved.expiresAt,now()+7*24*3600000);const originalPrivate=client.b64(client.pair.privateKey);
    const restored=vm.runInContext('new GitHubClient()',scope);restored.file=async()=>({value:{public_key:saved.serverKey}});
    assert.equal(await restored.restore(),true);assert.equal(restored.clientId,saved.clientId);assert.equal(restored.b64(restored.pair.privateKey),originalPrivate,'restore the decryption key with the token');
    restored.disconnect();assert.equal(sessionStorage.values.size,0);assert.equal(localStorage.getItem('wxread-session-v1'),null);
  }
  {
    const {scope,client,sessionStorage,now,advance}=setup();client.remember=true;client.sessionExpires=now()+1000;client.saveSession();advance(1001);
    const restored=vm.runInContext('new GitHubClient()',scope);assert.equal(await restored.restore(),false);assert.equal(sessionStorage.values.size,0);
    sessionStorage.setItem('wxread-session-v1','bad JSON');assert.equal(await restored.restore(),false);assert.equal(sessionStorage.values.size,0);
  }
  {
    const {scope,client,sessionStorage,now}=setup();client.remember=true;client.sessionExpires=now()+3600000;client.saveSession();const raw=sessionStorage.getItem('wxread-session-v1');
    scope.fetch=async()=>{throw new TypeError('offline');};const restored=vm.runInContext('new GitHubClient()',scope);restored.delay=async()=>{};
    await assert.rejects(restored.restore(),e=>e.code==='NETWORK');assert.equal(restored.connected,true);assert.equal(sessionStorage.getItem('wxread-session-v1'),raw,'keep a saved session during an outage');
  }
  {
    const {scope,client}=setup();scope.fetch=async()=>response(200,{default_branch:'main'});client.file=async()=>({value:{public_key:client.serverKey}});client.command=async()=>({overview:{accounts:[]}});
    scope.sessionStorage=scope.localStorage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');},removeItem(){throw new Error('blocked');}};
    assert.equal((await client.connect('test/repo','fixture-token',()=>{},true)).saved,false);assert.equal(client.connected,true);
    assert.equal(await vm.runInContext('new GitHubClient()',scope).restore(),false);
  }
  {
    const {scope,client,advance}=setup();advance(7*3600000);let inspections=0,finish;
    const overview={accounts:[],runs:[],statistics:{today:{running:0}}};
    client.command=async()=>{inspections++;return new Promise(resolve=>finish=resolve);};client.file=async()=>null;scope.fetch=async()=>response(200,{workflow_runs:[]});
    const first=client.overview(),second=client.overview();assert.equal(inspections,1,'coalesce renewal of an expired snapshot subscription');finish({overview});await Promise.all([first,second]);assert.equal(inspections,1);assert.equal(client.registeredAt,scope.Date.now());
  }
  {
    const {client}=setup();let reads=0,starts=0;client.start=async()=>{starts++;return 'operation';};client.state=async()=>{reads++;if(reads===1)throw client.error('offline','NETWORK',true);return {status:'success'};};
    assert.equal((await client.command('inspect')).status,'success');assert.equal(starts,1,'resume reading the same operation after an outage');assert.equal(reads,2);
  }
  {
    const {scope,client}=setup();let calls=0;
    scope.fetch=async()=>{calls++;return calls===1?response(401):response(200,{ok:true});};
    assert.equal((await client.request('')).ok,true);assert.equal(client.connected,true,'one transient 401 does not discard the session');
  }
  {
    const {scope,client}=setup();let calls=0;
    scope.fetch=async(url,options)=>{calls++;if(calls===1)return response(200,{value:1},{etag:'fixture-etag'});assert.equal(options.headers['If-None-Match'],'fixture-etag');return response(304);};
    const first=await client.request('/cached');first.value=42;
    assert.equal((await client.request('/cached')).value,1,'reuse an unmodified ETag response without sharing mutable data');
  }
  {
    const {scope,client}=setup();let active=0,max=0,calls=0,finish;
    scope.fetch=async()=>{active++;max=Math.max(max,active);calls++;await new Promise(resolve=>finish=resolve);active--;return response(200);};
    const a=client.request('/first'),same=client.request('/first'),b=client.request('/second');
    assert.equal(a,same,'coalesce simultaneous reads of the same resource');await Promise.resolve();assert.equal(calls,1);finish();await a;await Promise.resolve();await Promise.resolve();assert.equal(calls,2);finish();await b;assert.equal(max,1,'serialize GitHub requests');
  }
  {
    const {client}=setup();assert.equal(client.acceptOverview({revision:4,accounts:[{id:'third'}]}),true);
    assert.equal(client.acceptOverview({revision:3,accounts:[]}),false);assert.equal(client.data.accounts.length,1,'an older snapshot cannot erase a newly added account');
    assert.equal(client.acceptOverview({revision:5,accounts:[]}),true);
  }
  console.log('GitHub session, request recovery and subscription regression checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
