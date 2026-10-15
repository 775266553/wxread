const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

let clock = 100000;
class Clock extends Date { static now() { return clock; } }
const scope = vm.createContext({window:{}, Date:Clock});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../pages/static/github.js'), 'utf8'), scope);
const client = scope.window.githubClient;
const id = 'a'.repeat(32);
let response, run, runReads = 0;
client.decrypt = value => value;
client.file = async () => response ? {value:{ciphertext:response}} : null;
client.request = async () => {runReads++; return {workflow_runs:run?[run]:[]};};

async function main() {
  client.commands.set(id, {operation:'qr', run:null, lastRunCheck:0, completedSeenAt:null});
  run = {id:1, display_title:'wxread-control '+id, status:'in_progress'};
  response = {status:'waiting', qr:'first', qr_version:1, expires_at:'2000-01-01T00:00:00Z'};
  assert.equal((await client.state(id)).status, 'waiting', 'ignore the old synthetic expiry deadline');
  response = {status:'waiting', qr:'second', qr_version:2};
  assert.equal((await client.state(id)).qr, 'second', 'read replacements even with the same waiting status');
  assert.equal(runReads, 1, 'cache workflow status while polling QR responses');
  clock += 16000;
  run = {...run, status:'completed', conclusion:'failure'};
  let state = await client.state(id);
  assert.equal(state.status, 'refreshing');
  assert.equal(state.qr, null, 'remove stale QR when the worker has ended');
  clock += 16000;
  assert.equal((await client.state(id)).status, 'failed');
  response = {status:'success', overview:{accounts:[]}};
  assert.equal((await client.state(id)).status, 'success', 'a terminal encrypted response wins over job status');
  response = {status:'waiting', qr:'old'};
  clock += 16000;
  run = {...run, conclusion:'cancelled'};
  state = await client.state(id);
  assert.equal(state.status, 'cancelled');
  assert.equal(state.qr, null);
  run={...run,status:'in_progress'};client.commands.get(id).run=run;
  response={status:'waiting',qr:'stale',qr_version:3,refresh_id:'old'};
  client.commands.get(id).refreshId='new';client.commands.get(id).refreshCreated=clock;
  state=await client.state(id);assert.equal(state.status,'refreshing');assert.equal(state.qr,null,'never render the pre-refresh QR while awaiting acknowledgement');
  response={...response,qr:'fresh',qr_version:4,refresh_id:'new'};
  assert.equal((await client.state(id)).qr,'fresh');
  response.qr_expires_at=(clock-1)/1000;assert.equal((await client.state(id)).qr,null,'hide expired images even when GitHub returns an old response');
  {
    let finish;client.id=()=> 'b'.repeat(32);client.encrypt=()=> 'encrypted-fixture';client.b64=()=> 'fixture';
    scope.sodium={from_string:value=>value};
    response={status:'waiting',qr:'previous',refresh_id:'new'};
    client.file=async path=>path.startsWith('requests/')?null:{value:{ciphertext:response}};
    client.request=async(path,method)=>method==='PUT'?new Promise(resolve=>finish=resolve):{workflow_runs:[run]};
    const refreshing=client.refreshQR(id);await Promise.resolve();
    const duringWrite=await client.state(id);assert.equal(duringWrite.qr,null,'hide the old QR while its refresh request is still being uploaded');
    finish();await refreshing;assert.equal((await client.state(id)).qr,null);
    response={status:'waiting',qr:'replacement',refresh_id:'b'.repeat(32)};assert.equal((await client.state(id)).qr,'replacement');
  }
  console.log('GitHub QR client regression checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
