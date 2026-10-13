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
  clock += 11000;
  run = {...run, status:'completed', conclusion:'failure'};
  let state = await client.state(id);
  assert.equal(state.status, 'refreshing');
  assert.equal(state.qr, null, 'remove stale QR when the worker has ended');
  clock += 11000;
  assert.equal((await client.state(id)).status, 'failed');
  response = {status:'success', overview:{accounts:[]}};
  assert.equal((await client.state(id)).status, 'success', 'a terminal encrypted response wins over job status');
  response = {status:'waiting', qr:'old'};
  clock += 11000;
  run = {...run, conclusion:'cancelled'};
  state = await client.state(id);
  assert.equal(state.status, 'cancelled');
  assert.equal(state.qr, null);
  console.log('GitHub QR client regression checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
