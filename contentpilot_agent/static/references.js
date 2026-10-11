/* Reference text is untrusted; insert it only with textContent. */
const referenceState = {rows: [], selected: new Map(), offset: 0, filter: ''};
const referenceNav = button('参考帖二创', () => switchView('references'));
referenceNav.dataset.view = 'references';
referenceNav.prepend(icon('search'));
$('nav').insertBefore(referenceNav, $('[data-view=growth]'));
const jobReferenceSection = el('fieldset');
jobReferenceSection.append(el('legend', '二创参考帖（可选，最多 5 条）'));
const jobReferenceList = el('div');jobReferenceList.id = 'job-reference-list';
const guidanceLabel = el('label', '二创要求（可选）');
const guidance = el('textarea');guidance.name = 'rewrite_guidance';guidance.maxLength = 1000;guidance.rows = 3;
guidance.placeholder = '例如：参考通勤场景，改为新手选购角度，结合自己的商品规格，不编造使用体验。';
guidanceLabel.append(guidance);
jobReferenceSection.append(jobReferenceList, el('p', '先在「参考帖二创」录入或搜索帖子；任务会保留所选正文与链接快照。', 'small muted'), guidanceLabel);
$('#job-form').insertBefore(jobReferenceSection, $('#job-form .primary'));

function resetReferenceUI(){referenceState.rows=[];referenceState.selected.clear();referenceState.offset=0;referenceState.filter='';$('#references-list').replaceChildren();$('#job-reference-list').replaceChildren();$('#reference-form').reset();$('#reference-search-form').reset();guidance.value='';updateReferenceSelection();updateExternalSearch();}
function updateExternalSearch(){$('#reference-external-search').href='https://www.xiaohongshu.com/search_result?keyword='+encodeURIComponent($('#reference-keyword').value.trim())+'&source=web';}
function updateReferenceSelection(){$('#reference-selection-count').textContent='已选 '+referenceState.selected.size+' / 5 条';}
function clearReferenceSelection(){referenceState.selected.clear();updateReferenceSelection();}
function toggleReference(note,input){if(input.checked){if(referenceState.selected.size>=5&&!referenceState.selected.has(note.id)){input.checked=false;notice('最多选择 5 条参考帖');return;}referenceState.selected.set(note.id,note);}else referenceState.selected.delete(note.id);updateReferenceSelection();}
function renderReferences(){
 const list=$('#references-list');list.replaceChildren();
 if(!referenceState.rows.length)list.append(emptyState('还没有参考帖','输入关键词去小红书搜索，再录入可使用的参考内容；接通搜索服务后可自动导入。'));
 for(const note of referenceState.rows){const card=el('article',undefined,'panel reference-card'),label=el('label',undefined,'check'),input=el('input');input.type='checkbox';input.checked=referenceState.selected.has(note.id);input.setAttribute('aria-label','选择参考帖：'+note.title);input.addEventListener('change',()=>toggleReference(note,input));label.append(input,el('strong',note.title));const source=el('a','查看原帖 ↗');source.href=note.url;source.target='_blank';source.rel='noopener noreferrer';card.append(label,el('p',({gateway:'搜索服务导入',tikhub:'小红书数据服务 · TikHub',manual:'手动录入'}[note.source_type]||'搜索服务导入')+' · '+(note.keyword||'未填写关键词')+' · '+(note.author||'作者未填写'),'small muted'),details('查看参考正文',note.body));const controls=el('div',undefined,'review-actions');controls.append(source,button('删除',async()=>{if(!confirm('删除这条参考帖？已有任务快照仍会保留。'))return;await api('/references/'+note.id,'DELETE');referenceState.selected.delete(note.id);await loadReferences();}));card.append(controls);list.append(card);}
 $('#previous-references').disabled=referenceState.offset===0;$('#next-references').disabled=referenceState.rows.length<50;$('#references-page').textContent='第 '+(referenceState.offset/50+1)+' 页';updateReferenceSelection();
}
async function loadReferences(){
 const owner=state.user?.id;if(!owner)return;
 const configured=!!state.capabilities?.reference_search?.xiaohongshu;
 $('#reference-search-submit').disabled=!configured;
 $('#reference-search-filters').hidden=state.capabilities?.reference_search_service?.provider!=='tikhub';
 updateSearchNote();
 const rows=await api('/references?offset='+referenceState.offset+'&keyword='+encodeURIComponent(referenceState.filter));
 if(state.user?.id!==owner)return;referenceState.rows=rows;renderReferences();
}
function updateSearchNote(){const configured=!!state.capabilities?.reference_search?.xiaohongshu,provider=state.capabilities?.reference_search_service?.provider,limit=Number($('#reference-search-form').elements.limit.value);$('#reference-search-note').textContent=provider==='tikhub'?(configured?'已配置小红书数据服务（TikHub）。本次最多 1 次搜索 + '+limit+' 次正文读取，会使用服务额度；每分钟 1 次、每天 30 次搜索。':'自动搜索未接通。已完成小红书搜索适配，尚缺数据服务（TikHub）密钥，请管理员配置；也可先去小红书搜索并手动录入参考帖。'):(configured?'已配置搜索服务，结果会保存到参考库；每分钟 1 次、每天 30 次。配置不代表已验证可用。':'自动搜索未接通。请管理员配置小红书搜索数据服务，或先去小红书搜索并手动录入参考帖。');}
async function loadJobReferences(){
 const owner=state.user?.id,rows=await api('/references');if(state.user?.id!==owner)return;
 const options=new Map([...referenceState.selected,...rows.map(note=>[note.id,note])]);
 const root=$('#job-reference-list');root.replaceChildren();
 if(!options.size){root.append(el('p','暂未选择参考帖，当前任务将按商品资料生成。','small muted'));return;}
 for(const note of options.values()){const label=el('label',undefined,'check'),input=el('input');input.type='checkbox';input.name='reference_ids';input.value=note.id;input.checked=referenceState.selected.has(note.id);input.addEventListener('change',()=>{if($$('input:checked',root).length>5){input.checked=false;notice('最多选择 5 条参考帖');}});label.append(input,document.createTextNode(note.title));root.append(label);}
 if(referenceState.selected.size){$$('input[name=platforms]',$('#job-form')).forEach(input=>{input.checked=input.value==='xiaohongshu';});}
}
$('#reference-keyword').addEventListener('input',updateExternalSearch);
$('#reference-search-form').elements.limit.addEventListener('change',updateSearchNote);
$('#import-reference').addEventListener('click',()=>{$('#reference-form').reset();$('#reference-form').elements.keyword.value=$('#reference-keyword').value.trim();$('#reference-dialog').showModal();});
$('#rewrite-selected').addEventListener('click',()=>{if(!referenceState.selected.size){notice('请先选择至少 1 条参考帖');return;}openNewJob().catch(error=>notice(error.message));});
$('#refresh-references').addEventListener('click',()=>{referenceState.offset=0;referenceState.filter='';loadReferences().catch(error=>notice(error.message));});
$('#filter-references').addEventListener('click',()=>{referenceState.offset=0;referenceState.filter=$('#reference-keyword').value.trim();loadReferences().catch(error=>notice(error.message));});
$('#previous-references').addEventListener('click',()=>{referenceState.offset=Math.max(0,referenceState.offset-50);loadReferences().catch(error=>notice(error.message));});
$('#next-references').addEventListener('click',()=>{referenceState.offset+=50;loadReferences().catch(error=>notice(error.message));});
formHandler('#reference-form',async(form,data)=>{const owner=state.user?.id;await api('/references','POST',Object.fromEntries(data));if(state.user?.id!==owner)return;$('#reference-dialog').close();referenceState.offset=0;referenceState.filter='';notice('参考帖已保存');await loadReferences();});
formHandler('#reference-search-form',async(form,data)=>{const owner=state.user?.id;const result=await api('/references/search','POST',{keyword:data.get('keyword'),limit:Number(data.get('limit')),sort_type:data.get('sort_type'),note_type:data.get('note_type')});if(state.user?.id!==owner)return;referenceState.offset=0;referenceState.filter='';notice(result.posts.length?'已导入 '+result.posts.length+' 条参考帖':'搜索服务没有返回相关帖子');await loadReferences();});
updateReferenceSelection();updateExternalSearch();
