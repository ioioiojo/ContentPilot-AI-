/* Publishing, independent human labels, cumulative observations, and queued media. */
function percent(value){return value==null?'—':(value*100).toFixed(2)+'%';}
function safeLink(label,url){const a=el('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}
function contentOptions(form,j){form.reset();form.elements.job_id.value=j.id;form.elements.revision.value=j.revision;const select=form.elements.content_index;select.replaceChildren();j.contents.forEach((c,i)=>{const option=el('option',(i+1)+' · '+platforms[c.platform]+' · '+c.title.slice(0,30));option.value=i;select.append(option);});}
async function openPublication(j){const form=$('#publish-form');contentOptions(form,j);form.elements.request_key.value=crypto.randomUUID();await loadPublishAssets(j);$('#publish-dialog').showModal();}
function openEvaluation(j){contentOptions($('#evaluation-form'),j);$('#evaluation-dialog').showModal();}
function openMedia(j){const form=$('#media-form');contentOptions(form,j);for(const option of form.elements.kind.options)option.disabled=!state.capabilities.media[option.value].configured;form.elements.kind.value=[...form.elements.kind.options].find(o=>!o.disabled)?.value||'storyboard';$('#media-capabilities-note').textContent=Object.entries(state.capabilities.media).map(([k,v])=>({storyboard:'分镜',image:'图片',video:'视频'}[k])+'：'+(v.configured?'已配置':'未连接')).join(' · ');$('#media-dialog').showModal();}
function openMetrics(pub){const form=$('#metrics-form');form.reset();form.elements.publication_id.value=pub.id;for(const key of ['impressions','clicks','conversions','revenue','cost','evidence'])form.elements[key].value=pub.metrics?.[key]??(key==='evidence'?'':0);const date=new Date();form.elements.captured_at.value=new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);$('#metrics-dialog').showModal();}
function publicationCard(pub){
 const card=el('article',undefined,'product-card'),head=el('div',undefined,'publication-head');
 const names={published:pub.channel==='manual'?'已登记发布':'已发布',sending:'待核对',unknown:'未确认'};
 head.append(el('h3',pub.content.title),el('span',names[pub.status]||pub.status,'badge '+(pub.status==='published'?'approved':'pending_review')));
 card.append(head,el('p',platforms[pub.platform]+' · '+(pub.channel==='manual'?'手动登记':'授权网关')+' · '+(pub.experiment||'未分组')+' / '+pub.variant,'small muted'));
 if(pub.error)card.append(el('p',pub.error,'error'));
 if(pub.metrics){const block=el('div',undefined,'publication-metrics');block.append(el('p','曝光 '+pub.metrics.impressions+' · 点击 '+pub.metrics.clicks+' · 成交 '+pub.metrics.conversions,'small'),el('p','CTR '+percent(pub.metrics.ctr)+' · 转化率 '+percent(pub.metrics.conversion_rate)+' · ROI '+percent(pub.metrics.roi),'small'));card.append(block,el('p','截至 '+formatTime(pub.metrics.captured_at)+' · 依据：'+pub.metrics.evidence,'small muted'));}
 else card.append(el('p','尚未录入效果数据','capability-note'));
 if(pub.collected_metrics){const m=pub.collected_metrics;card.append(el('p','自动采集：点赞 '+(m.likes??'—')+' · 评论 '+(m.comments??'—')+' · 收藏 '+(m.collects??'—')+' · 分享 '+(m.shares??'—'),'small'),el('p','截至 '+formatTime(m.captured_at)+' · '+m.evidence,'small muted'));}
 const actions=el('div',undefined,'card-actions');if(pub.url)actions.append(safeLink('打开发布链接 ↗',pub.url));if(pub.status==='published')actions.append(button('录入效果',()=>openMetrics(pub)),button('自动采集',()=>openCollection(pub)));card.append(actions);return card;
}
async function loadGrowth(){
 const report=await api('/growth');const metrics=$('#growth-metrics');metrics.replaceChildren();
 for(const [label,value,sub,name] of [['已录入效果',report.observed_samples,'有累计数据的发布记录','file'],['点击率 CTR',percent(report.totals.ctr),'点击数 / 曝光数','chart'],['点击成交率',percent(report.totals.conversion_rate),'成交数 / 点击数','check'],['营收成本 ROI',percent(report.totals.roi),'营收减总成本 / 总成本','chart']])metrics.append(metricCard(label,value,sub,name));
 const quality=$('#growth-evaluation');quality.replaceChildren(el('h2','人工质量评测'),el('p','样本 '+report.evaluation.sample_size+' · 通过率 '+percent(report.evaluation.pass_rate)+' · 事实错误率 '+percent(report.evaluation.factual_error_rate)),el('p',report.evaluation.source,'small muted'));
 const a=el('a','导出人工标注集（JSONL）');a.href='/api/evaluations/export';a.download='';quality.append(a);
 const experiments=$('#growth-experiments');experiments.replaceChildren(el('h2','内容版本对比'));
 if(!report.experiments.length)experiments.append(el('p','还没有对比数据。登记发布时填写同一个对比组、不同版本标签，再录入效果。','muted small'));
 else{const scroll=el('div',undefined,'table-scroll'),table=el('table',undefined,'comparison-table'),head=el('thead'),row=el('tr'),body=el('tbody');table.setAttribute('aria-label','同平台内容版本效果对比');for(const label of ['对比组 / 平台','版本','样本','CTR','转化率','ROI']){const th=el('th',label);th.scope='col';row.append(th);}head.append(row);for(const e of report.experiments){const r=el('tr');for(const value of [e.experiment+' / '+platforms[e.platform],e.variant,e.samples,percent(e.ctr),percent(e.conversion_rate),percent(e.roi)])r.append(el('td',String(value)));body.append(r);}table.append(head,body);scroll.append(table);experiments.append(scroll);}
 experiments.append(el('p',report.caveat,'capability-note'));
 const pubs=$('#growth-publications');pubs.replaceChildren(el('h2','发布记录'));if(!report.publications.length)pubs.append(emptyState('让第一条内容走向发布','在已审核任务中登记发布链接，再录入数据开始追踪。'));
 const grid=el('div',undefined,'product-grid');for(const pub of report.publications)grid.append(publicationCard(pub));pubs.append(grid);if(report.truncated)pubs.append(el('p','页面统计仅覆盖最近1000条发布记录。','small muted'));
}
async function renderJobOperations(j,root){
 if(!j.contents||j.is_owner===false)return;
 const panel=el('section',undefined,'panel spaced');panel.append(el('h3','评测、素材与发布'));
 const controls=el('div',undefined,'review-actions');controls.append(button('人工质量标注',()=>openEvaluation(j)));
 if(j.status==='approved'){
   const canMedia=Object.values(state.capabilities?.media||{}).some(x=>x.configured);const media=button('生成素材 / 分镜',()=>openMedia(j));media.disabled=!canMedia;if(!canMedia)media.title='素材服务尚未配置';
   controls.append(media,button('登记 / 发布内容',()=>openPublication(j),'primary'));
 }else panel.append(el('p','人工审核通过后，可生成素材并登记发布。','small muted'));
 panel.append(controls);
 if(j.status==='approved')panel.append(el('p',Object.entries(state.capabilities.media).map(([k,v])=>({storyboard:'AI 分镜',image:'图片生成',video:'视频生成'}[k])+' · '+(v.configured?'已配置':'未连接')).join('　/　'),'capability-note'));
 panel.append(details('本次技能与效果参考',{技能:j.request.skill||{},历史效果:j.request.performance_context||[],工具模式:j.request.tools_mode||'local'}));
 const [labels,assets,pubs]=await Promise.all([api('/jobs/'+j.id+'/evaluations'),api('/jobs/'+j.id+'/media'),api('/publications?job_id='+j.id)]);
 for(const label of labels){panel.append(el('p','第 '+(label.content_index+1)+' 条 · 版本 '+label.revision+' · 人工评测 '+(label.passed?'通过':'待改善')+' · 事实/平台/实用 '+[label.facts_score,label.platform_score,label.usefulness_score].join('/')+' · '+label.note,'small'));if(label.factual_errors.length)panel.append(el('p','事实问题：'+label.factual_errors.join('；'),'error'));}
 for(const asset of assets){const div=el('div',undefined,'asset-row');div.append(el('p',({storyboard:'分镜方案',image:'图片',video:'视频'}[asset.kind])+' · '+({queued:'排队中',running:'生成中',completed:'已完成',failed:'未完成'}[asset.status])+' · 版本 '+asset.revision));if(asset.error)div.append(el('p',asset.error,'error small'));if(asset.result)div.append(details('素材说明',asset.result));if(asset.status==='completed'){const a=el('a','下载素材');a.href='/api/media/'+asset.id+'/download';a.download='';div.append(a);}panel.append(div);}
 if(assets.some(a=>['queued','running'].includes(a.status)))panel.append(button('刷新素材状态',()=>openJob(j.id)));
 for(const pub of pubs)panel.append(publicationCard(pub));root.append(panel);
}
formHandler('#publish-form',async(form,data)=>{const value=Object.fromEntries(data);value.revision=Number(value.revision);value.content_index=Number(value.content_index);value.media_ids=data.getAll('media_ids');if(value.channel==='gateway'){const platform=state.current.contents[value.content_index].platform;if(!state.capabilities.publishing[platform])throw Error('该平台未配置网关，请先手动发布并登记链接');if(!confirm('将已审核文案和所选素材发送到'+platforms[platform]+'的授权发布网关？'))return;}const pub=await api('/publications','POST',value);$('#publish-dialog').close();notice(pub.status==='published'?'发布记录已保存':'发布结果待核对，不会自动重发');await openJob(value.job_id);});
formHandler('#metrics-form',async(form,data)=>{const value=Object.fromEntries(data);const id=value.publication_id;delete value.publication_id;for(const k of ['impressions','clicks','conversions','revenue','cost'])value[k]=Number(value[k]);value.captured_at=new Date(value.captured_at).getTime()/1000;await api('/publications/'+id+'/metrics','POST',value);$('#metrics-dialog').close();notice('效果快照已保存，下一轮选题将参考这些数据');if(state.view==='growth')await loadGrowth();if($('#detail-dialog').open)await openJob(state.current.id);});
formHandler('#evaluation-form',async(form,data)=>{const value=Object.fromEntries(data);const id=value.job_id;delete value.job_id;for(const k of ['revision','content_index','facts_score','platform_score','usefulness_score'])value[k]=Number(value[k]);value.factual_errors=value.factual_errors.split('\n').map(x=>x.trim()).filter(Boolean);await api('/jobs/'+id+'/evaluations','POST',value);$('#evaluation-dialog').close();notice('人工评测已保存');await openJob(id);});
formHandler('#media-form',async(form,data)=>{const value=Object.fromEntries(data);const id=value.job_id;delete value.job_id;value.revision=Number(value.revision);value.content_index=Number(value.content_index);await api('/jobs/'+id+'/media','POST',value);$('#media-dialog').close();notice('素材任务已排队，生成后请人工核对');await openJob(id);});
$('#refresh-growth').addEventListener('click',()=>loadGrowth().catch(e=>notice(e.message)));
