/* GitHub-only control plane. No API server, browser storage of Tokens, or plaintext repository state. */
'use strict';
class GitHubClient {
  constructor() { this.token='';this.repository='';this.branch='';this.pair=null;this.clientId='';this.commands=new Map();this.data=null;this.pendingReading=null; }
  get connected() {return Boolean(this.token && this.pair && this.branch);}
  id() {return crypto.randomUUID().replaceAll('-','');}
  b64(value) {return sodium.to_base64(value,sodium.base64_variants.ORIGINAL);}
  bytes(value) {return sodium.from_base64(value,sodium.base64_variants.ORIGINAL);}
  encrypt(value,key) {return this.b64(sodium.crypto_box_seal(sodium.from_string(JSON.stringify(value)),this.bytes(key)));}
  decrypt(value) {return JSON.parse(sodium.to_string(sodium.crypto_box_seal_open(this.bytes(value),this.pair.publicKey,this.pair.privateKey)));}
  async request(path,method='GET',body,missing=false) {
    const response=await fetch('https://api.github.com/repos/'+this.repository+path,{method,cache:'no-store',
      headers:{Accept:'application/vnd.github+json',Authorization:'Bearer '+this.token,'X-GitHub-Api-Version':'2026-03-10',...(body===undefined?{}:{'Content-Type':'application/json'})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    if(missing && response.status===404)return null;
    if(response.status===401)this.disconnect();
    if(!response.ok){const errors={401:'GitHub Token 无效或已过期',403:'GitHub 权限不足或请求受限，请检查 Token 和 Actions 设置',404:'找不到仓库或工作流，请先将本次代码推送到默认分支',409:'仓库正在更新，请稍后重试',422:'GitHub 拒绝此操作，请确认工作流已发布且允许手动触发'};throw new Error(errors[response.status]||`GitHub 请求失败（${response.status}）`);}
    const text=await response.text();return text?JSON.parse(text):null;
  }
  async file(path,ref='wxread-state') {
    const result=await this.request('/contents/'+path+'?ref='+encodeURIComponent(ref)+'&t='+Date.now(),'GET',undefined,true);
    if(!result)return null;
    return {value:JSON.parse(sodium.to_string(this.bytes(result.content.replace(/\s/g,'')))),sha:result.sha};
  }
  async connect(repository,token,progress=()=>{}) {
    await sodium.ready;
    if(!/^[\w.-]+\/[\w.-]+$/.test(repository))throw new Error('仓库格式为 owner/repository');
    this.repository=repository;this.token=token.trim();this.pair=sodium.crypto_box_keypair();this.clientId=this.id();
    try {
      progress('正在验证仓库权限…');const repo=await this.request('');this.branch=repo.default_branch;
      await this.request('/actions/workflows/control.yml');
      let metadata=await this.file('control.json');
      if(!metadata){progress('正在初始化加密密钥…');await this.initialize();metadata=await this.file('control.json');}
      this.serverKey=metadata.value.public_key;
      progress('正在等待 Actions 读取账户配置，通常需要数十秒…');
      const result=await this.command('inspect',{},state=>progress(state.message||'等待 Actions 启动…'));
      this.data=result.overview;
      localStorage.setItem('wxread-repository',repository);
    }catch(error){this.disconnect();throw error;}
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
    if(state?.overview)this.data=state.overview;
    if(state && ['success','failed','expired','cancelled'].includes(state.status))return state;
    if(Date.now()-command.lastRunCheck>=10000){
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
    return state||{status:'starting',qr:null,message:run?.status==='waiting'?'Actions 正在等待环境审批，请到 GitHub Actions 批准此运行…':run?.status==='in_progress'?'Actions 正在准备浏览器和账户配置…':'等待 GitHub Actions 启动，请保持页面打开…'};
  }
  async command(operation,options={},progress=()=>{}) {
    const id=await this.start(operation,options),deadline=Date.now()+12*60*1000;
    while(Date.now()<deadline){const state=await this.state(id);progress(state);if(state.status==='success')return state;if(['failed','expired','cancelled'].includes(state.status))throw new Error(state.message||'操作未完成');await new Promise(r=>setTimeout(r,5000));}
    throw new Error('等待 Actions 超时，请查看 GitHub 运行记录；操作可能仍在排队');
  }
  async cancel(id) {
    const command=this.commands.get(id);if(!command)return;
    if(!command.runId){const runs=await this.request('/actions/workflows/control.yml/runs?event=workflow_dispatch&per_page=30');command.runId=runs.workflow_runs.find(r=>r.display_title==='wxread-control '+id)?.id;}
    if(command.runId)await this.request('/actions/runs/'+command.runId+'/cancel','POST',{});
    this.commands.delete(id);
  }
  async overview() {
    const file=await this.file('responses/'+this.clientId+'.json');if(file)this.data=this.decrypt(file.value.ciphertext).overview;
    if(!this.data)throw new Error('账户状态尚未就绪');
    const data=structuredClone(this.data),result=await this.request('/actions/workflows/deploy.yml/runs?per_page=50');
    const map=new Map(result.workflow_runs.map(r=>[String(r.id),r]));
    for(const run of data.runs){const workflow=map.get(String(run.workflow_run_id));if(workflow?.status==='completed' && ['running','queued'].includes(run.status)){run.status=workflow.conclusion==='cancelled'?'cancelled':'interrupted';run.message='Actions 已结束，显示最后保存的任务进度';}}
    if(this.pendingReading){
      const pending=this.pendingReading,run=result.workflow_runs.find(r=>r.display_title===pending.title && new Date(r.created_at).getTime()>=pending.created-5000);
      if(run?.status==='completed' || data.runs.some(r=>r.workflow_run_id===String(run?.id))){this.pendingReading=null;}else{
        for(const id of pending.accountIds){const account=data.accounts.find(a=>a.id===id);if(account)data.runs.unshift({id:'pending-'+id,account_id:id,account_name:account.name,status:run?.status==='in_progress'?'running':'queued',completed:0,target:account.read_num,message:'等待 Actions 保存任务进度',started_at:new Date(pending.created).toISOString(),workflow_run_id:run?.id});}
      }
    }
    data.workflow_runs=result.workflow_runs.slice(0,20);
    const today=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
    data.statistics.today.running=data.runs.filter(r=>r.status==='running' && new Date(r.started_at).toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'})===today).length;
    return data;
  }
  async run(accountId='') {
    const runs=await this.request('/actions/workflows/deploy.yml/runs?per_page=20');
    if(runs.workflow_runs.some(r=>r.status!=='completed'))throw new Error('已有阅读工作流运行或排队，请先完成或停止本批任务');
    await this.request('/actions/workflows/deploy.yml/dispatches','POST',{ref:this.branch,inputs:{account_id:accountId}});
    this.pendingReading={title:'wxread-reading '+(accountId||'all'),created:Date.now(),accountIds:accountId?[accountId]:this.data.accounts.filter(a=>a.enabled).map(a=>a.id)};
    return {started:this.pendingReading.accountIds,skipped:[]};
  }
  async stop(accountId) {const data=await this.overview(),run=data.runs.find(r=>r.account_id===accountId && ['queued','running'].includes(r.status));if(!run?.workflow_run_id)throw new Error('任务尚未分配运行编号，请稍后重试');await this.request('/actions/runs/'+run.workflow_run_id+'/cancel','POST',{});}
  disconnect() {if(this.pair)sodium.memzero(this.pair.privateKey);this.token='';this.pair=null;this.branch='';this.data=null;this.pendingReading=null;this.commands.clear();}
  url(path='') {return 'https://github.com/'+this.repository+path;}
}
window.githubClient=new GitHubClient();
