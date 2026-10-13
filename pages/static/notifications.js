'use strict';
const notificationLabels={pushplus:'PushPlus',wxpusher:'WxPusher',telegram:'Telegram',serverchan:'Server酱',webhook:'Webhook'};
const notificationFields={pushplus:[['token','Token']],wxpusher:[['spt','SPT']],telegram:[['bot_token','Bot Token'],['chat_id','Chat ID']],serverchan:[['send_key','SendKey']]};
function activeNotificationNames(state) {
  return [...(state.email?.enabled?['邮件']:[]),...Object.keys(notificationLabels).filter(key=>state.notifications?.[key]?.enabled).map(key=>notificationLabels[key])];
}
function notificationCards(settings={}) {
  return ['webhook','pushplus','wxpusher','telegram','serverchan'].map(channel=>{
    const label=notificationLabels[channel];
    const value=settings[channel]||{};
    const fields=channel==='webhook'?`
      <label>机器人类型<select name="format"><option value="wecom" ${value.format==='wecom'?'selected':''}>企业微信</option><option value="feishu" ${value.format==='feishu'?'selected':''}>飞书</option><option value="dingtalk" ${value.format==='dingtalk'?'selected':''}>钉钉</option><option value="json" ${value.format==='json'?'selected':''}>通用 JSON Webhook</option></select></label>
      <label>Webhook 地址<input name="url" type="password" autocomplete="new-password" placeholder="${value.has_url?'已保存；留空保留原地址':'粘贴机器人的 HTTPS Webhook 地址'}"><small>地址加密保存，重新打开页面时不显示原值。</small></label>
      <div data-webhook-signature ${['feishu','dingtalk'].includes(value.format)?'':'hidden'}><label>签名密钥（可选）<input name="secret" type="password" autocomplete="new-password" placeholder="${value.has_secret?'已保存；留空保留原密钥':'机器人开启签名校验时填写'}"></label>${value.has_secret?'<label class="toggle-label"><span>清除已保存的签名密钥</span><input name="clear_secret" type="checkbox"></label>':''}</div>
      <details><summary>自定义 HTTP 请求头（可选）</summary><label>请求头 JSON<textarea name="headers" rows="3" placeholder="${value.has_headers?'已保存；留空保留原请求头':'例如：{&quot;Authorization&quot;:&quot;Bearer ...&quot;}'}"></textarea></label>${value.has_headers?'<label class="toggle-label"><span>清除已保存的请求头</span><input name="clear_headers" type="checkbox"></label>':''}<small>企业微信、飞书、钉钉通常无需额外请求头。</small></details>
      <p class="section-description">在群聊中添加自定义机器人，复制 Webhook 地址。若启用关键词，可设置“微信读书”；飞书、钉钉开启加签时还需填写签名密钥。</p>`:
      notificationFields[channel].map(([field,title])=>`<label>${title}<input name="${field}" type="${field==='chat_id'?'text':'password'}" autocomplete="new-password" placeholder="${value['has_'+field]?'已保存；留空保留原值':'填写'+title}"></label>`).join('');
    return `<details class="panel notification-panel" ${value.enabled||channel==='webhook'?'open':''}><summary class="panel-head"><div><h2>${label} 通知<span>${value.enabled?'已开启':'未开启'}</span></h2><p>${channel==='webhook'?'企业微信、飞书、钉钉与自建服务':'可与邮件、其他渠道同时启用'}</p></div>${icon('arrow')}</summary><form id="notification-form-${channel}" data-notification-channel="${channel}" class="settings-body"><label class="toggle-label"><span>开启 ${label} 通知</span><input name="enabled" type="checkbox" class="switch" ${value.enabled?'checked':''}></label>${fields}<div class="actions-group"><button class="button primary">保存 ${label} 设置</button><button type="button" class="button" data-test-notification="${channel}">${icon('mail')}发送测试通知</button></div><p id="notification-result-${channel}" class="form-error" role="status"></p></form></details>`;
  }).join('');
}
function notificationValues(form) {
  const settings=Object.fromEntries(new FormData(form));
  settings.enabled=form.elements.enabled.checked;
  for(const field of ['clear_secret','clear_headers'])if(form.elements[field])settings[field]=form.elements[field].checked;
  return {channel:form.dataset.notificationChannel,settings};
}
document.addEventListener('change',event=>{
  const select=event.target.closest('form[data-notification-channel="webhook"] select[name="format"]');
  if(select)select.form.querySelector('[data-webhook-signature]').hidden=!['feishu','dingtalk'].includes(select.value);
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-test-notification]');if(!button)return;
  const channel=button.dataset.testNotification,result=document.querySelector('#notification-result-'+channel);
  button.disabled=true;result.textContent='正在发送测试通知…';
  try {const response=await api('/settings/notifications/test','POST',notificationValues(button.form));result.textContent=response.message||'测试请求已接受，请检查接收端';}
  catch(error){result.textContent=error.message;}
  finally{button.disabled=false;}
});
