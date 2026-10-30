export interface Unit {text:string;page:number|null;section:string;extraction_method:'native'|'ocr';bounding_box:[number,number,number,number]|null;ocr_confidence:number|null;quality_flags:string[];human_edited:boolean}
export interface Job {job_id:string;filename:string;content_hash:string;status:string;revision:number;created_at:number;updated_at:number;preview_hash:string;page_count:number;unit_count:number;reason:string;document_id:string;document_version:string}
export interface Preview {job:Job;parsed:{units:Unit[];pages:{page:number;method:string;block_count:number;quality_flags:string[]}[]}}
export interface Source {ref_id:string;title:string;excerpt:string;page:number|null;document_id:string;document_version:string;bounding_box:[number,number,number,number]|null;extraction_method:string;human_edited:boolean}
export interface Answer {answer:string;sources:Source[];trace_id:string;stop_reason:string;degraded:boolean;trace:{worker:string;action:string;status:string;duration_ms:number}[];answer_mode:string}
export const labels:Record<string,string>={queued:'等待解析',parsing:'解析中',awaiting_review:'待核对',queued_commit:'等待入库',committing:'入库中',completed:'已入库',failed:'处理失败',cancelled:'已取消'};
export const active=(s:string)=>['queued','parsing','queued_commit','committing'].includes(s);
const errors:Record<string,string>={workspace_auth_required:'访问密钥无效，请重新连接。',workspace_token_not_configured:'服务端尚未配置访问密钥。',workspace_capacity_reached:'本地工作区容量已满，请先备份整理。',stale_review:'预览已变化，请重新加载后再确认。',source_changed:'原件已变化，当前审核已失效。',preview_changed:'预览校验失败，请重新处理。',ocr_models_invalid:'本地 OCR 模型缺失或校验失败。',ocr_timeout:'OCR 超时，可核对文件后重试。',pdf_page_requires_ocr:'此文档含扫描内容，需要服务端开启 OCR。',workspace_answer_busy:'另一个回答正在处理，请稍后重试。',page_renderer_busy:'原页图像正在生成，请稍后重试。',page_preview_unavailable:'原页图像暂不可用，可先核对识别正文。'};
export function message(code:string){return errors[code]||`操作未完成（${code}）`}
export async function api<T>(token:string,path:string,init:RequestInit={},signal?:AbortSignal):Promise<T>{
 const response=await fetch('/v1/workspace/'+path,{...init,signal,headers:{...init.headers,Authorization:'Bearer '+token},cache:'no-store',credentials:'omit'});
 if(!response.ok){let code='http_'+response.status;try{const body=await response.json();code=body.detail?.code||code}catch{}throw new Error(message(code))}
 return response.json() as Promise<T>;
}
export function json(value:unknown):RequestInit{return{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)}}
export function encodedName(name:string){return btoa(String.fromCharCode(...new TextEncoder().encode(name)))}
