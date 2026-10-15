/* GitHub-only control plane. Browser credential storage requires explicit opt-in. */
'use strict';
class GitHubClient {
  constructor() { this.token='';this.repository='';this.branch='';this.pair=null;this.clientId='';this.commands=new Map();this.data=null;this.pendingReading=null;this.sessionVersion=0;this.retryAt=0;this.remember=false;this.sessionExpires=0;this.registeredAt=0;this.renewal=null;this.storageKind='session';this.requestCache=new Map();this.inflight=new Map();this.requestQueue=Promise.resolve(); }
  get connected() {return Boolean(this.token && this.pair && this.branch);}
  id() {return crypto.randomUUID().replaceAll('-','');}
  b64(value) {return sodium.to_base64(value,sodium.base64_variants.ORIGINAL);}
  bytes(value) {return sodium.from_base64(value,sodium.base64_variants.ORIGINAL);}
  encrypt(value,key) {return this.b64(sodium.crypto_box_seal(sodium.from_string(JSON.stringify(value)),this.bytes(key)));}
  decrypt(value) {return JSON.parse(sodium.to_string(sodium.crypto_box_seal_open(this.bytes(value),this.pair.publicKey,this.pair.privateKey)));}
  error(message,code,retryable=false) {return Object.assign(new Error(message),{code,retryable});}
  assertSession(version) {if(version!==this.sessionVersion)throw this.error('连接已更换，已忽略旧请求','SESSION_CHANGED');}
  delay(ms) {return new Promise(resolve=>setTimeout(resolve,ms));}
  clearSavedSession() {for(const storage of ['sessionStorage','localStorage'])try{globalThis[storage].removeItem('wxread-session-v1');}catch{}}
  saveSession() {
    if(!this.remember)return true;
    if(this.sessionExpires<=Date.now()){this.clearSavedSession();this.remember=false;return false;}
    try {
      const storage=this.storageKind==='local'?localStorage:sessionStorage;
      storage.setItem('wxread-session-v1',JSON.stringify({version:1,storage:this.storageKind,repository:this.repository,token:this.token,
        branch:this.branch,clientId:this.clientId,serverKey:this.serverKey,publicKey:this.b64(this.pair.publicKey),privateKey:this.b64(this.pair.privateKey),
        expiresAt:this.sessionExpires,registeredAt:this.registeredAt}));
      return true;
    }catch{this.clearSavedSession();this.remember=false;return false;}
  }
  async restore() {
    await sodium.ready;
    let saved,pair;
    for(const kind of ['local','session']){
      let storage;
      try {
        storage=kind==='local'?localStorage:sessionStorage;
        const raw=storage.getItem('wxread-session-v1');if(!raw)continue;
        saved=JSON.parse(raw);
        if(saved.version!==1 || !/^[\w.-]+\/[\w.-]+$/.test(saved.repository) || typeof saved.token!=='string' || !saved.token.trim() ||
          typeof saved.branch!=='string' || !saved.branch || !/^[a-f0-9]{32}$/.test(saved.clientId) ||
          !Number.isFinite(saved.expiresAt) || saved.expiresAt<=Date.now() || saved.expiresAt>Date.now()+(kind==='local'?7*24:12)*60*60*1000 ||
          !Number.isFinite(saved.registeredAt) || saved.registeredAt>Date.now())throw new Error('invalid session');
        pair={publicKey:this.bytes(saved.publicKey),privateKey:this.bytes(saved.privateKey)};
        if(pair.publicKey.length!==32 || pair.privateKey.length!==32 || this.bytes(saved.serverKey).length!==32)throw new Error('invalid keys');
        this.storageKind=kind;
        if(kind==='local')try{sessionStorage.removeItem('wxread-session-v1');}catch{}
        break;
      }catch{if(pair)sodium.memzero(pair.privateKey);pair=null;saved=null;try{storage?.removeItem('wxread-session-v1');}catch{}}
    }
    if(!saved || !pair)return false;
    this.sessionVersion++;this.repository=saved.repository;this.token=saved.token;this.branch=saved.branch;this.clientId=saved.clientId;
    this.pair=pair;this.serverKey=saved.serverKey;this.registeredAt=saved.registeredAt;this.remember=true;this.sessionExpires=saved.expiresAt;
    // Verify authorization again; a temporary network failure keeps the session for retry.
    const version=this.sessionVersion,repo=await this.request('');this.assertSession(version);this.branch=repo.default_branch;
    const metadata=await this.file('control.json');this.assertSession(version);
    if(!metadata)throw this.error('缺少加密公钥配置，请检查 wxread-state 分支','CONFIGURATION');
    this.serverKey=metadata.value.public_key;this.saveSession();return true;
  }
  request(path,method='GET',body,missing=false) {
    const version=this.sessionVersion,key=method+' '+path;
    if(method==='GET' && this.inflight.has(key))return this.inflight.get(key);
    const work=()=>{this.assertSession(version);return this.performRequest(path,method,body,missing);};
    const task=this.requestQueue.then(work,work);this.requestQueue=task.catch(()=>{});
    if(method==='GET'){this.inflight.set(key,task);task.finally(()=>{if(this.inflight.get(key)===task)this.inflight.delete(key);}).catch(()=>{});}
    return task;
  }
  async performRequest(path,method='GET',body,missing=false) {
    const version=this.sessionVersion,repository=this.repository,token=this.token;
    const attempts=method==='GET'?3:1,cache=method==='GET'?this.requestCache.get(path):null;
    for(let attempt=0;attempt<attempts;attempt++){
      this.assertSession(version);
      if(Date.now()<this.retryAt)throw this.error('GitHub 请求限流，稍后自动恢复；无需重新登录','RATE_LIMIT',true);
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
      let response,text;
      try {
        response=await fetch('https://api.github.com/repos/'+repository+path,{method,cache:'no-store',signal:controller.signal,
          headers:{Accept:'application/vnd.github+json',Authorization:'Bearer '+token,'X-GitHub-Api-Version':'2026-03-10',...(cache?.etag?{'If-None-Match':cache.etag}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},
          ...(body===undefined?{}:{body:JSON.stringify(body)})});
        text=await response.text();
      }catch{
        this.assertSession(version);
        if(attempt+1<attempts){await this.delay(1000*2**attempt);continue;}
        throw this.error(method==='GET'?'网络暂时不可用，正在等待恢复；无需重新登录':'请求结果未确认，请查看 Actions 记录后再重试，避免重复提交','NETWORK',true);
      }finally{clearTimeout(timeout);}
      this.assertSession(version);
      if(missing && response.status===404)return null;
      if(response.status===304 && cache)return structuredClone(cache.value);
      if(response.status===401 && method==='GET' && attempt===0){await this.delay(1000);continue;}
      if(response.status===401){this.disconnect();throw this.error('GitHub Token 无效、已过期或被撤销，请重新连接','AUTHENTICATION');}
      const retryAfter=response.headers.get('retry-after'),remaining=response.headers.get('x-ratelimit-remaining');
      if(response.status===429 || (response.status===403 && (retryAfter!==null || remaining==='0' || /rate limit/i.test(text)))){
        const reset=Number(response.headers.get('x-ratelimit-reset'))*1000;
        const retrySeconds=Number(retryAfter);
        this.retryAt=Math.max(Date.now()+60000,Number.isFinite(retrySeconds)?Date.now()+retrySeconds*1000:0,remaining==='0' && Number.isFinite(reset)?reset:0);
        throw this.error('GitHub 请求限流，稍后自动恢复；无需重新登录','RATE_LIMIT',true);
      }
      if(response.status>=500 && attempt+1<attempts){await this.delay(1000*2**attempt);continue;}
      if(!response.ok){const errors={403:'GitHub 权限不足，请检查此 Token 的仓库权限',404:'找不到仓库或工作流，请检查仓库和默认分支',409:'仓库正在更新，请稍后重试',422:'GitHub 拒绝此操作，请确认工作流已发布且允许手动触发'};throw this.error(errors[response.status]||`GitHub 暂时无法完成请求（${response.status}）`,response.status>=500?'NETWORK':'PERMISSION',response.status>=500);}
      const value=text?JSON.parse(text):null;
      if(method==='GET'){this.requestCache.set(path,{etag:response.headers.get('etag'),value:structuredClone(value)});if(this.requestCache.size>80)this.requestCache.delete(this.requestCache.keys().next().value);}
      return value;
    }
  }
  async file(path,ref='wxread-state') {
    const result=await this.request('/contents/'+path+'?ref='+encodeURIComponent(ref),'GET',undefined,true);
    if(!result)return null;
    return {value:JSON.parse(sodium.to_string(this.bytes(result.content.replace(/\s/g,'')))),sha:result.sha};
  }
  async connect(repository,token,progress=()=>{},remember=false) {
    await sodium.ready;
    if(!/^[\w.-]+\/[\w.-]+$/.test(repository))throw new Error('仓库格式为 owner/repository');
    this.disconnect();const version=this.sessionVersion;
    this.repository=repository;this.token=token.trim();this.pair=sodium.crypto_box_keypair();this.clientId=this.id();
    try {
      progress('正在验证仓库权限…');const repo=await this.request('');this.assertSession(version);this.branch=repo.default_branch;
      await this.request('/actions/workflows/control.yml');
      let metadata=await this.file('control.json');
      if(!metadata){progress('正在初始化加密密钥…');await this.initialize();metadata=await this.file('control.json');}
      this.serverKey=metadata.value.public_key;
      progress('正在等待 Actions 读取账户配置，通常需要数十秒…');
      const result=await this.command('inspect',{},state=>progress(state.message||'等待 Actions 启动…'));
      this.assertSession(version);
      this.acceptOverview(result.overview);
      this.remember=remember;this.storageKind='local';this.sessionExpires=Date.now()+7*24*60*60*1000;
      const saved=this.saveSession();
      try{localStorage.setItem('wxread-repository',repository);}catch{}
      return {saved};
    }catch(error){if(version===this.sessionVersion)this.disconnect();throw error;}
  }
  async initialize() {
    if(await this.file('state.enc.json'))throw new Error('已有加密账户状态，但缺少公钥配置。请恢复 control.json，避免覆盖现有密钥。');
    const pair=sodium.crypto_box_keypair();
    try {
      const repositoryKey=await this.request('/actions/secrets/public-key');
      const encrypted=this.b64(sodium.crypto_box_seal(sodium.from_string(this.b64(pair.privateKey)),this.bytes(repositoryKey.key)));
      await this.request('/actions/secrets/WXREAD_CONTROL_KEY','PUT',{encrypted_value:encrypted,key_id:repositoryKey.key_id});
      const metadata={version:1,public_key:this.b64(pair.publicKey)};
      const ref=await this.request('/git/ref/heads/wxread-state','GET',undefined,true);
      if(!ref){
        const tree=await this.request('/git/trees','POST',{tree:[{path:'control.json',mode:'100644',type:'blob',content:JSON.stringify(metadata)}]});
        const commit=await this.request('/git/commits','POST',{message:'Initialize encrypted wxread control',tree:tree.sha,parents:[]});
        await this.request('/git/refs','POST',{ref:'refs/heads/wxread-state',sha:commit.sha});
      }else{
        await this.request('/contents/control.json','PUT',{message:'Initialize wxread control',branch:'wxread-state',content:this.b64(sodium.from_string(JSON.stringify(metadata)))});
      }
    }finally{sodium.memzero(pair.privateKey);}
  }
  async start(operation,options={}) {
    if(!this.connected)throw new Error('请先连接 GitHub 仓库');
    const id=this.id(), key=this.b64(this.pair.publicKey);
    const payload={...options,_client:{id:this.clientId,public_key:key}};
    const encrypted=this.encrypt(payload,this.serverKey);
    if(encrypted.length>60000)throw new Error('配置内容过大，请分批导入账户');
    await this.request('/actions/workflows/control.yml/dispatches','POST',{ref:this.branch,inputs:{operation,session_id:id,public_key:key,payload:encrypted}});
    this.commands.set(id,{id,operation,created:Date.now(),runId:null,run:null,lastRunCheck:0,completedSeenAt:null});
    return id;
  }
  async state(id) {
    const command=this.commands.get(id);
    if(!command)throw new Error('操作会话已结束，请重新提交');
    const file=await this.file('responses/'+id+'.json');
    const state=file?this.decrypt(file.value.ciphertext):null;
    if(state?.overview)this.acceptOverview(state.overview);
    if(state && ['success','failed','expired','cancelled'].includes(state.status)){if(state.status==='success'){this.registeredAt=Date.now();this.saveSession();}return state;}
    if(Date.now()-command.lastRunCheck>=15000){
      const runs=await this.request('/actions/workflows/control.yml/runs?event=workflow_dispatch&per_page=30');
      command.run=runs.workflow_runs.find(r=>r.display_title==='wxread-control '+id)||null;
      command.lastRunCheck=Date.now();if(command.run)command.runId=command.run.id;
    }
    const run=command.run;
    if(run?.status==='completed'){
      if(run.conclusion==='cancelled')return {status:'cancelled',message:'扫码任务已取消，请重新生成二维码',qr:null};
      command.completedSeenAt ??= Date.now();
      if(Date.now()-command.completedSeenAt<10000)return {status:'refreshing',message:'任务已结束，正在读取最终结果…',qr:null};
      return {status:'failed',message:`Actions 已结束（${run.conclusion}），未返回结果。请打开运行记录检查工作流。`,qr:null};
    }
    if(command.refreshId && state?.refresh_id!==command.refreshId){
      if(Date.now()-command.refreshCreated>45000)return {status:'failed',qr:null,message:'刷新未响应，请重新生成二维码'};
      return {status:'refreshing',qr:null,message:'正在刷新二维码…'};
    }
    if(command.operation==='qr' && state?.qr && state.qr_expires_at && Date.now()>=state.qr_expires_at*1000)return {...state,status:'refreshing',qr:null,message:'二维码已过期，等待更新…'};
    return state||{status:'starting',qr:null,message:run?.status==='waiting'?'Actions 正在等待环境审批，请到 GitHub Actions 批准此运行…':run?.status==='in_progress'?'Actions 正在准备浏览器和账户配置…':'等待 GitHub Actions 启动，请保持页面打开…'};
  }
  async command(operation,options={},progress=()=>{}) {
    const id=await this.start(operation,options),deadline=Date.now()+12*60*1000;
    const version=this.sessionVersion;
    while(Date.now()<deadline){
      this.assertSession(version);
      try{const state=await this.state(id);progress(state);if(state.status==='success')return state;if(['failed','expired','cancelled'].includes(state.status))throw new Error(state.message||'操作未完成');}
      catch(error){if(!error.retryable)throw error;progress({status:'retrying',message:error.message});await this.delay(Math.min(30000,Math.max(5000,this.retryAt-Date.now())));continue;}
      await this.delay(5000);
    }
    throw new Error('等待 Actions 超时，请查看 GitHub 运行记录；操作可能仍在排队');
  }
  acceptOverview(overview) {
    if(!overview)return false;
    if(this.data && Number(overview.revision||0)<Number(this.data.revision||0))return false;
    this.data=overview;return true;
  }
  async refreshQR(id) {
    const command=this.commands.get(id);if(!command || command.operation!=='qr')throw new Error('扫码会话已结束，请重新生成');
    const requestId=this.id(),path='requests/'+id+'.json';
    command.refreshId=requestId;command.refreshCreated=Date.now();
    const current=await this.file(path);
    const value={ciphertext:this.encrypt({action:'refresh',session_id:id,request_id:requestId},this.serverKey)};
    await this.request('/contents/'+path,'PUT',{branch:'wxread-state',message:'Refresh wxread login QR',content:this.b64(sodium.from_string(JSON.stringify(value))),...(current?{sha:current.sha}:{})});
    return requestId;
  }
  async cancel(id) {
    const command=this.commands.get(id);if(!command)return;
    if(!command.runId){const runs=await this.request('/actions/workflows/control.yml/runs?event=workflow_dispatch&per_page=30');command.runId=runs.workflow_runs.find(r=>r.display_title==='wxread-control '+id)?.id;}
    if(command.runId)await this.request('/actions/runs/'+command.runId+'/cancel','POST',{});
    this.commands.delete(id);
  }
  async overview() {
    if(!this.connected)throw this.error('请先连接 GitHub 仓库','AUTHENTICATION');
    if(Date.now()-this.registeredAt>=6*60*60*1000)await this.renewSnapshot();
    const file=await this.file('responses/'+this.clientId+'.json');if(file)this.acceptOverview(this.decrypt(file.value.ciphertext).overview);
    if(!this.data)await this.renewSnapshot();
    if(!this.data)throw new Error('账户状态尚未就绪');
    const result=await this.request('/actions/workflows/deploy.yml/runs?per_page=50'),data=structuredClone(this.data);
    const map=new Map(result.workflow_runs.map(r=>[String(r.id),r]));
    for(const run of data.runs){const workflow=map.get(String(run.workflow_run_id));if(workflow?.status==='completed' && ['running','queued'].includes(run.status)){run.status=workflow.conclusion==='cancelled'?'cancelled':'interrupted';run.message='Actions 已结束，显示最后保存的任务进度';}}
    if(this.pendingReading){
      const pending=this.pendingReading,run=result.workflow_runs.find(r=>r.display_title===pending.title && new Date(r.created_at).getTime()>=pending.created-5000);
      if(run?.status==='completed' || data.runs.some(r=>r.workflow_run_id===String(run?.id))){this.pendingReading=null;}else{
        for(const id of [...pending.accountIds].reverse()){const account=data.accounts.find(a=>a.id===id);if(account)data.runs.unshift({id:'pending-'+id,account_id:id,account_name:account.name,status:'queued',completed:0,target:account.read_num,message:'等待顺序执行',started_at:new Date(pending.created).toISOString(),workflow_run_id:run?.id});}
      }
    }
    data.workflow_runs=result.workflow_runs.slice(0,20);
    const today=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
    data.statistics.today.running=data.runs.filter(r=>r.status==='running' && new Date(r.started_at).toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'})===today).length;
    return data;
  }
  async renewSnapshot() {
    if(!this.renewal){const version=this.sessionVersion;const task=this.command('inspect').then(result=>{this.assertSession(version);this.acceptOverview(result.overview);this.registeredAt=Date.now();this.saveSession();});this.renewal=task;task.finally(()=>{if(this.renewal===task)this.renewal=null;}).catch(()=>{});}
    return this.renewal;
  }
  async run(accountId='') {
    const runs=await this.request('/actions/workflows/deploy.yml/runs?per_page=20');
    if(runs.workflow_runs.some(r=>r.status!=='completed'))throw new Error('已有阅读工作流运行或排队，请先完成或停止本批任务');
    await this.request('/actions/workflows/deploy.yml/dispatches','POST',{ref:this.branch,inputs:{account_id:accountId}});
    this.pendingReading={title:'wxread-reading '+(accountId||'all'),created:Date.now(),accountIds:accountId?[accountId]:this.data.accounts.filter(a=>a.enabled && a.status!=='needs_login').map(a=>a.id)};
    return {started:this.pendingReading.accountIds,skipped:[]};
  }
  async stop(accountId) {const data=await this.overview(),run=data.runs.find(r=>r.account_id===accountId && ['queued','running'].includes(r.status));if(!run?.workflow_run_id)throw new Error('任务尚未分配运行编号，请稍后重试');await this.request('/actions/runs/'+run.workflow_run_id+'/cancel','POST',{});}
  disconnect() {this.sessionVersion++;this.clearSavedSession();if(this.pair)sodium.memzero(this.pair.privateKey);this.token='';this.pair=null;this.branch='';this.data=null;this.pendingReading=null;this.commands.clear();this.remember=false;this.sessionExpires=0;this.registeredAt=0;this.retryAt=0;this.renewal=null;this.requestCache.clear();this.inflight.clear();this.requestQueue=Promise.resolve();}
  url(path='') {return 'https://github.com/'+this.repository+path;}
}
window.githubClient=new GitHubClient();
