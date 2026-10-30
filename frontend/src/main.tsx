import {useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {active,api,encodedName,json,labels,message,type Answer,type Job,type Preview,type Source} from './api';
import './style.css';

function App(){
 const [token,setToken]=useState(''),[draftKey,setDraftKey]=useState(''),[jobs,setJobs]=useState<Job[]>([]),[selected,setSelected]=useState('');
 const [preview,setPreview]=useState<Preview|null>(null),[page,setPage]=useState(1),[pageUrl,setPageUrl]=useState(''),[pageError,setPageError]=useState('');
 const [notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[question,setQuestion]=useState(''),[answer,setAnswer]=useState<Answer|null>(null),[asking,setAsking]=useState(false);
 const [edits,setEdits]=useState<Record<number,string>>({}),[reviewed,setReviewed]=useState(false),[events,setEvents]=useState<{event:string;revision:number;created_at:number}[]>([]);
 const fileRef=useRef<HTMLInputElement>(null),detailSerial=useRef(0),answerSerial=useRef(0),pageSerial=useRef(0),questionController=useRef<AbortController|null>(null);
 const sessionSerial=useRef(0),navigationSerial=useRef(0);
 const chosen=jobs.find(j=>j.job_id===selected),ready=jobs.filter(j=>j.status==='completed').length;

 async function connect(){const serial=++sessionSerial.current;try{const key=draftKey.trim();const rows=await api<Job[]>(key,'jobs');if(serial!==sessionSerial.current)return;setJobs(rows);setToken(key);setDraftKey('');setNotice('');setSelected(rows[0]?.job_id||'')}catch(e){if(serial===sessionSerial.current)setNotice(String((e as Error).message))}}
 function disconnect(){sessionSerial.current++;navigationSerial.current++;questionController.current?.abort();answerSerial.current++;detailSerial.current++;pageSerial.current++;setBusy(false);setAsking(false);setToken('');setJobs([]);setSelected('');setPreview(null);setAnswer(null);setQuestion('');setEdits({});setEvents([]);setNotice('');}
 useEffect(()=>{if(!token)return;const controller=new AbortController();let stopped=false,pending=false;
  async function refresh(){if(pending)return;pending=true;try{const rows=await api<Job[]>(token,'jobs',{},controller.signal);if(!stopped)setJobs(rows)}catch(e){if(!controller.signal.aborted&&!stopped)setNotice((e as Error).message)}finally{pending=false}}
  void refresh();const timer=setInterval(()=>void refresh(),2000);return()=>{stopped=true;controller.abort();clearInterval(timer)};
 },[token]);
 useEffect(()=>{const serial=++detailSerial.current;setPreview(null);setEvents([]);setEdits({});setReviewed(false);if(!token||!chosen)return;
  const controller=new AbortController();
  void api<typeof events>(token,`jobs/${chosen.job_id}/events`,{},controller.signal).then(v=>{if(serial===detailSerial.current)setEvents(v)}).catch(()=>{});
  if(['awaiting_review','queued_commit','committing','completed'].includes(chosen.status)){
   void api<Preview>(token,`jobs/${chosen.job_id}/preview`,{},controller.signal).then(v=>{if(serial===detailSerial.current){setPreview(v);setPage(p=>Math.min(Math.max(1,v.job.page_count),p))}}).catch(e=>{if(!controller.signal.aborted&&serial===detailSerial.current)setNotice((e as Error).message)});
  }
  return()=>controller.abort();
 },[token,chosen?.job_id,chosen?.revision]);
 useEffect(()=>{const serial=++pageSerial.current;setPageUrl('');setPageError('');if(!token||!preview)return;
  if(!/\.(pdf|png|jpe?g)$/i.test(preview.job.filename)){setPageError('此格式以识别正文核对，不生成页图。');return}
  const controller=new AbortController();let url='';
  void fetch(`/v1/workspace/jobs/${preview.job.job_id}/pages/${page}`,{signal:controller.signal,headers:{Authorization:'Bearer '+token},cache:'no-store',credentials:'omit'}).then(async r=>{if(!r.ok){let code='page_preview_unavailable';try{code=(await r.json()).detail?.code||code}catch{}throw Error(message(code))}return r.blob()}).then(blob=>{url=URL.createObjectURL(blob);if(serial===pageSerial.current&&!controller.signal.aborted)setPageUrl(url);else URL.revokeObjectURL(url)}).catch(e=>{if(!controller.signal.aborted&&serial===pageSerial.current)setPageError((e as Error).message)});
  return()=>{controller.abort();if(url)URL.revokeObjectURL(url)};
 },[token,preview?.job.job_id,page]);
 useEffect(()=>()=>questionController.current?.abort(),[]);

 async function upload(file:File){if(file.size>5*1024*1024){setNotice('单文件最多 5 MiB。');return}const serial=sessionSerial.current;navigationSerial.current++;setBusy(true);setNotice('');try{
  const job=await api<Job>(token,'uploads',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Upload-Name':encodedName(file.name)},body:file});
  if(serial!==sessionSerial.current)return;setJobs(rows=>[job,...rows.filter(j=>j.job_id!==job.job_id)]);setSelected(job.job_id);setPage(1);
 }catch(e){if(serial===sessionSerial.current)setNotice((e as Error).message)}finally{if(serial===sessionSerial.current){setBusy(false);if(fileRef.current)fileRef.current.value=''}}}
 async function action(kind:'confirm'|'cancel'|'retry'|'edit'){
  if(!chosen)return;const serial=sessionSerial.current;setBusy(true);setNotice('');const id=chosen.job_id;
  try{let result:Job;if(kind==='edit'){
    if(!preview)return;
    const resultPreview=await api<Preview>(token,`jobs/${id}/edit`,json({revision:preview.job.revision,preview_hash:preview.job.preview_hash,edits:Object.entries(edits).map(([i,text])=>({unit_index:Number(i),text}))}));result=resultPreview.job;
   }else result=await api<Job>(token,`jobs/${id}/${kind}`,kind==='confirm'?json({revision:preview?.job.revision,preview_hash:preview?.job.preview_hash}):{method:'POST'});
   if(serial===sessionSerial.current)setJobs(rows=>rows.map(j=>j.job_id===result.job_id?result:j));
  }catch(e){if(serial===sessionSerial.current)setNotice((e as Error).message)}finally{if(serial===sessionSerial.current)setBusy(false)}
 }
 async function ask(){if(!question.trim())return;questionController.current?.abort();const controller=new AbortController();questionController.current=controller;const serial=++answerSerial.current;setAsking(true);setAnswer(null);setNotice('');try{
  const result=await api<Answer>(token,'answer',json({question:question.trim(),knowledge_source:'catalog',kb_id:'workspace',task:'qa',answer_mode:'extractive',execution_mode:'workflow',max_steps:4}),controller.signal);
  if(serial===answerSerial.current)setAnswer(result);
 }catch(e){if(!controller.signal.aborted&&serial===answerSerial.current)setNotice((e as Error).message)}finally{if(serial===answerSerial.current)setAsking(false)}}
 async function locate(source:Source){const serial=++navigationSerial.current;try{const job=await api<Job>(token,`sources/${source.document_id}/${source.document_version}`);if(serial!==navigationSerial.current)return;setSelected(job.job_id);setPage(source.page||1);document.querySelector('#document-preview')?.scrollIntoView({behavior:'smooth',block:'start'})}catch(e){if(serial===navigationSerial.current)setNotice((e as Error).message)}}
 const pageUnits=preview?.parsed.units.map((u,i)=>({...u,index:i})).filter(u=>!u.page||u.page===page)||[];
 return <div className="shell">
  <aside className="rail"><div className="brand"><span className="brand-mark">P</span><span>PersonaX<small>LOCAL WORKSPACE</small></span></div><div className="nav-active">▤　文档与证据</div><div className="rail-note"><span className="dot"/> 本机工作区<p>原件、识别与检索留在本机。<br/>资料确认后才进入问答。</p></div></aside>
  <main><header><div><div className="eyebrow">KNOWLEDGE WORKSPACE</div><h1>让每一次回答，都能找到出处。</h1><p className="muted">上传资料，核对识别结果，再用证据回答问题。</p></div>{token&&<button className="quiet" onClick={disconnect}>断开连接</button>}</header>
   {notice&&<div className="notice" role="alert">{notice}<button onClick={()=>setNotice('')} aria-label="关闭提示">×</button></div>}
   {!token?<section className="login card"><div className="tag">仅限本机 · 单操作者</div><h2>连接你的文档工作台</h2><p>访问密钥见本地启动器提示的私有文件。密钥仅保留在当前页面内存，刷新后需要重新输入。</p><form onSubmit={e=>{e.preventDefault();void connect()}}><label htmlFor="access-key">工作台访问密钥</label><input id="access-key" type="password" value={draftKey} autoComplete="off" onChange={e=>setDraftKey(e.target.value)}/><button className="primary" disabled={!draftKey.trim()}>连接工作台 →</button></form></section>:<>
    <section className="stats"><div><strong>{jobs.length}</strong><span>导入任务</span></div><div><strong>{ready}</strong><span>已确认入库</span></div><div><strong>{jobs.filter(j=>j.status==='awaiting_review').length}</strong><span>等待人工核对</span></div><div className="stats-note">确定性导入 Workflow<span>只读问答 · 发布权限隔离</span></div></section>
    <div className="work-grid"><section className="card library"><div className="section-heading"><div><div className="eyebrow">01 / LIBRARY</div><h2>资料与处理任务</h2></div><span className="chip">本地持久化</span></div>
      <input ref={fileRef} type="file" id="file-upload" hidden accept=".pdf,.txt,.md,.html,.htm,.png,.jpg,.jpeg" onChange={e=>{const f=e.target.files?.[0];if(f)void upload(f)}}/>
      <button className="upload-zone" disabled={busy} onClick={()=>fileRef.current?.click()}><span className="upload-icon">↑</span><strong>{busy?'正在提交…':'选择一份受信资料'}</strong><span>PDF / 扫描件 / 图片 / MD / TXT / HTML</span><small>单文件 5 MiB · 扫描件需要人工核对</small></button>
      <div className="job-list">{!jobs.length?<div className="empty">还没有资料。上传后会在这里显示真实处理状态。</div>:jobs.map(j=><button key={j.job_id} className={'job '+(selected===j.job_id?'selected':'')} onClick={()=>{setSelected(j.job_id);setPage(1);setNotice('')}}><div className="job-title">{j.filename}</div><div className="job-meta"><span className={'status '+j.status}>{labels[j.status]}</span><span>{j.page_count?j.page_count+' 页':new Date(j.created_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</span></div></button>)}</div>
    </section>
    <section className="card document" id="document-preview"><div className="section-heading"><div><div className="eyebrow">02 / REVIEW</div><h2>{chosen?.filename||'核对文档'}</h2></div>{chosen&&<span className={'status '+chosen.status}>{labels[chosen.status]}</span>}</div>
      {!chosen?<div className="empty tall">选择左侧任务，查看解析状态与识别结果。</div>:<>
       <div className="task-line"><code>{chosen.job_id.slice(0,16)}</code><span>版本 {chosen.revision}</span>{active(chosen.status)&&<span className="spinner-text">● 后台处理中，可离开后返回</span>}</div>
       {chosen.reason&&<div className="inline-error">{message(chosen.reason)}</div>}
       {preview?<><div className="page-nav"><button onClick={()=>setPage(p=>Math.max(1,p-1))} disabled={page===1}>←</button><span>第 {page} / {Math.max(1,preview.job.page_count)} 页</span><button disabled={page>=Math.max(1,preview.job.page_count)} onClick={()=>setPage(p=>p+1)}>→</button><span className="muted">{pageUnits.length} 个正文块</span></div>
        <div className="review-grid"><div className="page-surface">{pageUrl?<div className="image-wrap"><img src={pageUrl} alt={`原件第 ${page} 页`}/>{pageUnits.filter(u=>u.bounding_box).map(u=>{const [x0,y0,x1,y1]=u.bounding_box!;return <button aria-label={`定位正文块 ${u.index+1}`} key={u.index} className="box" title={`正文块 ${u.index+1}`} style={{left:`${x0*100}%`,top:`${y0*100}%`,width:`${(x1-x0)*100}%`,height:`${(y1-y0)*100}%`}} onClick={()=>document.getElementById('unit-'+u.index)?.focus()}/>})}</div>:<div className="empty">{pageError||'正在加载受保护的原页…'}</div>}</div>
         <div className="text-blocks">{pageUnits.map(u=><div className="text-block" key={u.index}><label htmlFor={'unit-'+u.index}>正文块 {u.index+1}<span>{u.extraction_method==='ocr'?'OCR':'原生文本'}{u.human_edited?' · 人工修订':''}</span></label><textarea id={'unit-'+u.index} value={edits[u.index]??u.text} readOnly={chosen.status!=='awaiting_review'} onChange={e=>{setEdits(v=>({...v,[u.index]:e.target.value}));setReviewed(false)}}/><div className="quality">{u.ocr_confidence!==null&&<span>识别置信度 {(u.ocr_confidence*100).toFixed(1)}%（非准确率）</span>}{u.quality_flags.includes('low_confidence')&&<strong>低置信度，请逐字核对</strong>}</div></div>)}{!pageUnits.length&&<div className="empty">此页为空白页，没有入库正文。</div>}</div></div>
        {chosen.status==='awaiting_review'&&<div className="review-actions">{Object.keys(edits).length>0?<button className="primary" disabled={busy} onClick={()=>void action('edit')}>保存修订并更新审核版本</button>:<><label className="checkbox"><input type="checkbox" checked={reviewed} onChange={e=>setReviewed(e.target.checked)}/>我已核对当前正文及原件，确认用于检索</label><button className="primary" disabled={!reviewed||busy} onClick={()=>void action('confirm')}>确认当前版本入库 →</button></>}</div>}
       </>:<div className="empty tall">{chosen.status==='queued'?'等待 Worker 领取。若长时间不变，请检查启动器终端。':active(chosen.status)?'任务已保存在本机，刷新后仍可查看状态。这里不会展示虚构进度。':'当前任务尚无可核对的正文。'}</div>}
       <div className="task-actions">{chosen.status==='failed'&&<button disabled={busy} onClick={()=>void action('retry')}>重新处理</button>}{!['committing','completed','cancelled'].includes(chosen.status)&&<button className="quiet" disabled={busy} onClick={()=>void action('cancel')}>取消任务</button>}<details><summary>任务追踪 · {events.length} 条事件</summary><ol>{events.map((e,i)=><li key={i}><code>{e.event}</code><span>v{e.revision} · {new Date(e.created_at*1000).toLocaleTimeString('zh-CN')}</span></li>)}</ol></details></div>
      </>}
    </section></div>
    <section className="card assistant"><div className="section-heading"><div><div className="eyebrow">03 / EVIDENCE</div><h2>向已确认的资料提问</h2></div><span className="chip">原文证据模式</span></div><p className="muted">只检索已入库的资料。默认使用确定性检索与摘录，不需要大模型；保留现有只读 Agent API。</p><form className="ask-form" onSubmit={e=>{e.preventDefault();void ask()}}><textarea aria-label="向资料提问" placeholder="例如：资料中的审核要求是什么？" maxLength={2000} value={question} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();void ask()}}}/><button className="primary" disabled={asking||!question.trim()||ready===0}>{asking?'正在检索…':'查找证据 →'}</button></form>
     {answer&&<div className="answer"><div className="answer-text">{answer.answer}</div><div className="citations">{answer.sources.map(s=><button key={s.ref_id} onClick={()=>void locate(s)}><strong>[{s.ref_id}] {s.title}</strong><span>{s.page?`第 ${s.page} 页`:'原文正文'} · {s.extraction_method==='ocr'?'OCR':'文本'}{s.human_edited?' · 人工修订':''} ↗</span><p>{s.excerpt}</p></button>)}</div><details><summary>回答追踪 · {answer.trace_id}</summary><p className="muted">模式：{answer.answer_mode} · 停止原因：{answer.stop_reason||'workflow'}</p><ol>{answer.trace.map((t,i)=><li key={i}><code>{t.worker} / {t.action}</code><span>{t.status} · {t.duration_ms.toFixed(0)} ms</span></li>)}</ol></details></div>}
    </section><footer>PersonaX · 本地文档工作台 P1 <span>受信资料 · 人工确认 · 引用可追溯</span></footer>
   </>}
  </main>
 </div>
}
createRoot(document.getElementById('root')!).render(<App/>);
