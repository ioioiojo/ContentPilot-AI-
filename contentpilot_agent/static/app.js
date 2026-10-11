/* All user and model text is inserted with textContent, never raw HTML. */
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = {csrf: '', user: null, view: 'jobs', offset: 0, current: null, products: []};
const statuses = {queued:'排队中',running:'执行中',pending_review:'待人工审核',approved:'已通过',rejected:'已驳回',failed:'执行失败'};
const platforms = {xiaohongshu:'小红书',douyin:'抖音',wechat:'公众号'};
const stages = {queued:'等待执行',starting:'启动任务',plan:'选题完成',generate:'初稿完成',review:'审核完成',revise:'修改完成',finished:'已完成',retry_wait:'等待重试',failed:'执行失败',worker_lost:'进程中断',plan_started:'正在选题',generate_started:'正在生成',review_started:'正在审核',revise_started:'正在修改'};
const actions = {save:'保存编辑',approve:'审核通过',reject:'驳回修改'};
let pollBusy = false;

function icon(name){const n=document.createElementNS('http://www.w3.org/2000/svg','svg');n.setAttribute('class','icon');n.setAttribute('aria-hidden','true');const use=document.createElementNS(n.namespaceURI,'use');use.setAttribute('href','#icon-'+name);n.append(use);return n;}
function metricCard(label,value,sub,name='chart'){const card=el('div',undefined,'metric');const glyph=el('div',undefined,'metric-icon');glyph.append(icon(name));card.append(el('span',label),glyph,el('strong',String(value)));if(sub)card.append(el('span',sub));return card;}
function emptyState(title,text){const box=el('div',undefined,'empty');box.append(icon('file'),el('h3',title),el('p',text));return box;}
function renderJobs(){
 const all=state.pageJobs||[],query=$('#job-search').value.trim().toLocaleLowerCase(),status=$('#job-status').value;
 const jobs=all.filter(j=>(!query||j.product_name.toLocaleLowerCase().includes(query))&&(!status||j.status===status));
 const list=$('#jobs-list');list.replaceChildren();$('#job-list-count').textContent='本页 '+jobs.length+' / '+all.length+' 个任务';
 if(!jobs.length)list.append(emptyState(all.length?'没有匹配的任务':'还没有内容任务',all.length?'换个关键词或状态，再试一次。':'先在「商品资料」添加真实商品，再创建你的第一份内容。'));
 for(const j of jobs){const row=el('div',undefined,'job-row'),main=el('div',undefined,'job-main'),glyph=el('div',undefined,'job-icon'),text=el('div');glyph.append(icon('file'));text.append(el('h3',j.product_name),el('p',formatTime(j.created_at)+' · '+(stages[j.stage]||j.stage)+' · 执行 '+j.attempts+'/'+j.max_attempts+' 次','small muted'));if(j.error)text.append(el('p',j.error,'error small'));main.append(glyph,text);const right=el('div',undefined,'row-actions');right.append(badge(j.status),button('查看任务',()=>openJob(j.id)));row.append(main,right);list.append(row);}
}
function taskProgress(j){
 const labels=['任务提交','AI 起草','自动审核','人工确认','已通过审核'];
 const active=j.status==='approved'?4:['pending_review','rejected'].includes(j.status)?3:j.status==='running'?(/review|revise|finished/.test(j.stage)?2:1):0;
 const row=el('div',undefined,'task-progress');row.setAttribute('aria-label','任务进度');
 labels.forEach((label,i)=>{const step=el('div',undefined,'progress-step'+(i<active?' done':i===active?' current':''));step.append(el('span',i<active?'✓':String(i+1)),document.createTextNode(label));if(i===active)step.setAttribute('aria-current','step');row.append(step);});
 return row;
}
function contentTabs(j,root,editable){
 const nav=el('div',undefined,'content-tabs');nav.setAttribute('role','tablist');nav.setAttribute('aria-label','生成文案');
 const panels=j.contents.map((item,i)=>{const panel=editor(item,i,!editable);panel.id='content-panel-'+i;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby','content-tab-'+i);return panel;});
 const choose=index=>{state.contentIndex=index;[...nav.children].forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;panels[i].hidden=i!==index;});};
 j.contents.forEach((item,i)=>{const tab=button(String(i+1).padStart(2,'0')+' · '+platforms[item.platform],()=>choose(i));tab.id='content-tab-'+i;tab.setAttribute('role','tab');tab.setAttribute('aria-controls',panels[i].id);tab.addEventListener('keydown',e=>{let next;if(e.key==='ArrowRight')next=(i+1)%panels.length;if(e.key==='ArrowLeft')next=(i-1+panels.length)%panels.length;if(e.key==='Home')next=0;if(e.key==='End')next=panels.length-1;if(next!==undefined){e.preventDefault();choose(next);nav.children[next].focus();}});nav.append(tab);});
 root.append(nav,...panels);choose(Math.min(state.contentIndex||0,panels.length-1));
}

function el(tag, text, cls) { const n=document.createElement(tag); if(text !== undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(message) {const n=$('#toast');n.textContent=message;n.hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>n.hidden=true,5000);}
function formatTime(epoch){return new Date(epoch*1000).toLocaleString('zh-CN');}
function badge(status){return el('span',statuses[status]||status,'badge '+status);}
function button(label, handler, cls='quiet'){const b=el('button',label,cls);b.type='button';b.addEventListener('click',()=>Promise.resolve(handler()).catch(e=>notice(e.message)));return b;}
function details(title, data){const d=el('details');d.append(el('summary',title),el('pre',typeof data==='string'?data:JSON.stringify(data,null,2)));return d;}
async function api(path,method='GET',body){
  const response=await fetch('/api'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},...(body===undefined?{}:{body:JSON.stringify(body)})});
  let value;try{value=await response.json();}catch{value={detail:'服务器暂时不可用，请稍后重试'};}
  if(!response.ok){if(response.status===401 && path!='/login')showLogin();const message=Array.isArray(value.detail)?value.detail.map(x=>x.msg).join('；'):value.detail;throw new Error(message||'请求失败');}
  return value;
}
function showLogin(){state.user=null;state.csrf='';state.current=null;state.capabilities=null;state.pageJobs=[];state.contentIndex=0;state.products=[];state.offset=0;$('#job-search').value='';$('#job-status').value='';$('#workspace').hidden=true;$('#login-screen').hidden=false;$$('dialog[open]').forEach(d=>d.close());for(const id of ['jobs-list','products-list','users-list','detail-body','metrics','growth-metrics','growth-evaluation','growth-experiments','growth-publications'])$('#'+id).replaceChildren();for(const id of ['product-form','job-form','password-form','user-form','publish-form','metrics-form','evaluation-form','media-form'])$('#'+id).reset();if(typeof resetReferenceUI==='function')resetReferenceUI();if(typeof resetExtensions==='function')resetExtensions();}
async function startSession(info){state.user=info.user;state.csrf=info.csrf;state.capabilities=await api('/capabilities');$('#username').textContent=info.user.username;$('#user-avatar').textContent=info.user.username.slice(0,2);$('#admin-nav').hidden=!info.user.admin;if($('#services-nav'))$('#services-nav').hidden=!info.user.admin;$('#mode').textContent=info.provider==='mock'?'Mock 演示模式':'真实模型模式';$('#mode-note').textContent=info.provider==='mock'?'当前使用模拟内容与预设分数，用于体验流程。话题为本地示例；人工评测和效果数据单独保存。':'AI 起草后请核对商品事实。人工审核通过后，可生成素材、登记发布并跟踪效果。';$('#login-screen').hidden=true;$('#workspace').hidden=false;await switchView('jobs');}
async function switchView(view){state.view=view;for(const v of ['jobs','products','references','growth','account','users','knowledge','review','services'])$('#view-'+v).hidden=v!==view;$$('nav button').forEach(b=>{b.classList.toggle('selected',b.dataset.view===view);if(b.dataset.view===view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});$('#page-title').textContent={jobs:'任务工作台',products:'商品资料',references:'参考帖二创',growth:'发布与效果',account:'账号设置',users:'成员管理',knowledge:'知识库与热点',review:'审核中心',services:'服务配置'}[view];if(view==='jobs')await refreshJobs();if(view==='products')await loadProducts();if(view==='users')await loadUsers();if(view==='growth')await loadGrowth();if(view==='references')await loadReferences();if(view==='services')await loadServices();if(view==='knowledge')await loadKnowledge();if(view==='review')await loadReview();}
async function refreshJobs(){
 const [jobs,stats]=await Promise.all([api('/jobs?offset='+state.offset),api('/stats')]);
 const metrics=$('#metrics');metrics.replaceChildren();
 const total=Object.values(stats.counts).reduce((a,b)=>a+b,0);
 for(const [label,value,sub,name] of [['全部任务',total,'当前账号的内容任务','file'],['待审核',stats.counts.pending_review||0,'等你确认后再发布','clock'],['已通过',stats.counts.approved||0,'可生成素材或登记发布','check'],['平均生成耗时',stats.average_elapsed_ms===null?'—':(stats.average_elapsed_ms/1000).toFixed(2)+'s','最近 '+stats.sample_size+' 个完成任务','chart']])metrics.append(metricCard(label,value,sub,name));
 state.pageJobs=jobs;renderJobs();
 $('#previous-jobs').disabled=state.offset===0;$('#next-jobs').disabled=jobs.length<50;$('#page-number').textContent='第 '+(state.offset/50+1)+' 页';
}
async function loadProducts(){state.products=await api('/products');const list=$('#products-list');list.replaceChildren();if(!state.products.length)list.append(el('div','添加真实商品说明，让每个任务有资料可查。','empty'));for(const p of state.products){const card=el('article',undefined,'product-card');card.append(el('h3',p.name),el('p',p.facts,'facts'),el('p','来源：'+p.source,'small muted'));const controls=el('div',undefined,'review-actions');controls.append(button('编辑资料',()=>openProduct(p)),button('删除',async()=>{if(confirm('删除该商品资料？已有任务快照仍会保留。')){await api('/products/'+p.id,'DELETE');await loadProducts();}},'quiet danger'));card.append(controls);list.append(card);}}
function openProduct(product=null){const form=$('#product-form');form.reset();for(const name of ['id','name','facts','source'])form.elements[name].value=product?.[name]||'';$('#product-dialog').showModal();}
async function openNewJob(){state.products=await api('/products');if(!state.products.length){await switchView('products');openProduct();notice('请先添加一份商品资料');return;}const select=$('#product-select');select.replaceChildren();for(const p of state.products){const option=el('option',p.name);option.value=p.id;select.append(option);}await loadJobReferences();$('#job-dialog').showModal();}
function editor(item,index,disabled){const wrapper=el('article',undefined,'content-editor');wrapper.dataset.index=index;wrapper.append(el('h3',String(index+1).padStart(2,'0')+' / '+platforms[item.platform]));for(const [key,label,type] of [['title','标题','input'],['body','正文 / 脚本','textarea'],['call_to_action','行动建议','input'],['hashtags','标签（逗号分隔）','input']]){const l=el('label',label);const input=el(type);input.name=key;input.value=key==='hashtags'?item[key].join(', '):item[key];input.disabled=disabled;if(type==='textarea')input.rows=5;l.append(input);wrapper.append(l);}return wrapper;}
async function openJob(id){
 const j=await api('/jobs/'+id);if(state.current?.id!==j.id)state.contentIndex=0;state.current=j;$('#detail-title').textContent=j.product_name;const root=$('#detail-body');root.replaceChildren();const meta=el('div',undefined,'detail-meta');meta.append(badge(j.status),el('span',(stages[j.stage]||j.stage)+' · 执行 '+j.attempts+'/'+j.max_attempts,'muted'));root.append(meta);if(j.status!=='failed')root.append(taskProgress(j));
 if(j.error)root.append(el('p',j.error,'error'));
 root.append(details('商品资料与来源快照',j.source));
 if(j.request.references?.length)root.append(details('参考帖与二创要求',{要求:j.request.rewrite_guidance,参考帖:j.request.references}));
 if(j.result){const m=j.result.metrics;root.append(el('p','自动审核：'+j.result.review.score+' 分（'+(j.provider==='mock'?'预设演示分数':'模型评分')+'） · 修改 '+j.result.iterations+' 次 · '+(m.elapsed_ms/1000).toFixed(2)+' 秒','muted'));if(j.result.review.issues.length)root.append(el('p',j.result.review.issues.join('；'),'error'));root.append(details('模型调用统计',{模型调用次数:m.model_calls,Token用量:m.token_usage??'Mock 不调用模型',费用:m.estimated_cost??'未计费 / 未配置价格',续跑起点:m.resumed_from||'首次执行'}));if(j.result.plan)root.append(details(j.request.live_topics?.length?'选题计划（引用带来源热点）':'选题计划（话题为本地示例，非实时热点）',{选题:j.result.plan,参考话题:j.result.hot_topics,工具调用:j.result.tool_calls}));}
 let reviewToolbar;
 if(j.contents?.length){const editable=['pending_review','rejected'].includes(j.status);contentTabs(j,root,editable);if(editable){const l=el('label','审核说明（必填）');const note=el('textarea');note.id='review-note';note.rows=2;note.maxLength=2000;note.placeholder='记录核对依据与修改内容，再确认全部文案。';l.append(note);root.append(l);reviewToolbar=el('div',undefined,'review-actions review-toolbar');reviewToolbar.append(el('span','本次审核适用于全部 '+j.contents.length+' 条文案','muted'));for(const a of ['save','reject','approve']){const control=button(actions[a],()=>submitReview(a),a==='approve'?'primary':'quiet');if(a!=='save'&&j.assigned_reviewer&&j.assigned_reviewer!==state.user.id){control.disabled=true;control.title='请由已分配的审稿人审核';}reviewToolbar.append(control);}}if(j.status==='approved'&&j.is_owner){const a=el('a','下载已审核内容（JSON）','primary');a.href='/api/jobs/'+j.id+'/export';a.download='';root.append(a);}}
 if(j.is_owner&&j.status==='failed'&&j.checkpoint&&j.attempts<3)root.append(button('从断点继续生成',async()=>{if(!confirm('将从已保存的步骤继续，尚未完成的模型请求可能重新计费。继续？'))return;await api('/jobs/'+j.id+'/resume','POST');await openJob(j.id);await refreshJobs();},'primary'));
 else if(j.is_owner&&j.status==='failed'&&j.attempts<j.max_attempts)root.append(button('重试任务',async()=>{await api('/jobs/'+j.id+'/retry','POST');await openJob(j.id);await refreshJobs();},'primary'));
 await renderAssignment(j,root);
 await renderJobOperations(j,root);
 const events=el('details');events.append(el('summary','执行轨迹与内容版本（'+j.events.length+'）'));for(const e of j.events){const div=el('div',undefined,'event');div.append(el('p','第 '+e.attempt+' 次 · '+(stages[e.stage]||e.stage)+' · '+e.elapsed_ms+' ms'));if(Object.keys(e.payload).length)div.append(details('查看内容',e.payload));events.append(div);}root.append(events);
 const audits=el('details');audits.append(el('summary','人工审核记录（'+j.audit.length+'）'));for(const a of j.audit)audits.append(details(formatTime(a.created_at)+' · '+(actions[a.action]||'审核分配')+' · '+(a.reviewer||'')+' · 版本 '+a.revision,{说明:a.note,内容:a.contents}));root.append(audits);
 if(reviewToolbar)root.append(reviewToolbar);
 if(!$('#detail-dialog').open)$('#detail-dialog').showModal();
}
async function submitReview(action){const j=state.current;const note=$('#review-note').value.trim();if(!note){notice('请填写审核说明');return;}const contents=$$('.content-editor').map((node,i)=>({...j.contents[i],title:$('[name=title]',node).value,body:$('[name=body]',node).value,call_to_action:$('[name=call_to_action]',node).value,hashtags:$('[name=hashtags]',node).value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)}));await api('/jobs/'+j.id+'/review','POST',{revision:j.revision,action,note,contents});notice('已'+actions[action]);await openJob(j.id);await refreshJobs();}
async function loadUsers(){const [users,reviewers]=await Promise.all([api('/users'),api('/reviewers')]);const list=$('#users-list');list.replaceChildren(el('h2','团队成员'));for(const user of users){const row=el('div',undefined,'job-row');const qualified=reviewers.some(r=>r.id===user.id);row.append(el('span',user.username+(user.admin?' · 管理员':user.active?' · 已启用':' · 已停用')+(qualified?' · 审稿人':'')));row.append(button(qualified?'撤销审稿资格':'启用审稿资格',async()=>{await api('/admin/reviewers/'+user.id,'PUT',{enabled:!qualified});await loadUsers();}));if(!user.admin)row.append(button(user.active?'停用':'启用',async()=>{await api('/users/'+user.id,'PATCH',{active:!user.active});await loadUsers();}));list.append(row);}}
function formHandler(selector,handler){$(selector).addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;const submit=$('button[type=submit],button.primary',form);submit.disabled=true;try{await handler(form,new FormData(form));}catch(error){if(selector==='#login-form')$('#login-error').textContent=error.message;else notice(error.message);}finally{submit.disabled=false;}});}
formHandler('#login-form',async(form,data)=>{const info=await api('/login','POST',Object.fromEntries(data));form.reset();$('#login-error').textContent='';await startSession(info);});
formHandler('#product-form',async(form,data)=>{const values=Object.fromEntries(data),id=values.id;delete values.id;await api('/products'+(id?'/'+id:''),id?'PUT':'POST',values);$('#product-dialog').close();notice('商品资料已保存');await loadProducts();});
formHandler('#job-form',async(form,data)=>{const values=Object.fromEntries(data);values.platforms=data.getAll('platforms');values.reference_ids=data.getAll('reference_ids');values.content_count=Number(values.content_count);values.use_feedback=data.has('use_feedback');values.use_knowledge=data.has('use_knowledge');if(data.has('use_hotspots')&&!hotspotBatch)throw Error('请先在知识库与热点页面获取实时热点');values.hotspot_batch_id=data.has('use_hotspots')?hotspotBatch:null;delete values.use_hotspots;if(!values.platforms.length)throw Error('请至少选择一个平台');if(values.reference_ids.length>5)throw Error('最多选择 5 条参考帖');const j=await api('/jobs','POST',values);clearReferenceSelection();$('#job-dialog').close();notice('任务已加入队列');await switchView('jobs');await openJob(j.id);});
formHandler('#password-form',async(form,data)=>{await api('/password','POST',Object.fromEntries(data));form.reset();showLogin();notice('密码已修改，请重新登录');});
formHandler('#user-form',async(form,data)=>{await api('/users','POST',Object.fromEntries(data));form.reset();notice('成员已创建');await loadUsers();});
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view).catch(e=>notice(e.message))));
$$('.close-dialog').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
$('#new-product').addEventListener('click',()=>openProduct());$('#new-job').addEventListener('click',()=>openNewJob().catch(e=>notice(e.message)));
$('#job-search').addEventListener('input',renderJobs);$('#job-status').addEventListener('change',renderJobs);
$('#logout').addEventListener('click',async()=>{try{await api('/logout','POST');showLogin();}catch(e){notice(e.message);}});
$('#previous-jobs').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-50);refreshJobs().catch(e=>notice(e.message));});$('#next-jobs').addEventListener('click',()=>{state.offset+=50;refreshJobs().catch(e=>notice(e.message));});
setInterval(async()=>{if(!state.user||document.hidden||pollBusy)return;pollBusy=true;try{if(state.view==='jobs')await refreshJobs();if($('#detail-dialog').open&&['queued','running'].includes(state.current?.status))await openJob(state.current.id);}catch(e){notice(e.message);}finally{pollBusy=false;}},4000);
api('/me').then(startSession).catch(()=>showLogin());
