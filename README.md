# PersonaX 2.3.1 🍃 有界上下文、双重限流的内容 Agent

> 一个**真实可用**的小红书内容 Agent：LLM 多人格写作 × Skill 系统 × 合规管控 × Playwright 真实发布 × 定时调度 × 可视化工作台。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-green) ![LangChain](https://img.shields.io/badge/LangChain-Core%20%2B%20LangGraph-1C3C3C) ![Tests](https://img.shields.io/badge/Tests-124%20passed-brightgreen)

## ✨ 亮点（Highlights）

- **LLM 内容生成**：接入 DeepSeek，提示词资产化（`config/prompts.yaml` 热更新，改文案不动代码）
- **有状态 AI 助手**：真实流式回答，按需检索、可追溯引用、Agent Trace、SQLite 会话恢复与用户可控长期记忆
- **有界异步 LLM**：提供 `acomplete` / `acomplete_batch` / `acomplete_stream`，用 `asyncio.Semaphore` 限制在途 API 请求并记录吞吐、峰值并发和失败数
- **滑动窗口治理**：对话按最近消息数与估算 token 双重裁剪；Skill、真实发布和 LLM 请求频率使用精确滑动窗口
- **多人格系统**：`config/personas.yaml` 人格库，生成时 `--persona <名字>` 选谁用谁写（内置 4 个人格）
- **RAG 混合检索**：BM25 + dense embedding 精确 kNN 双路召回，RRF 融合；支持 query rewriting / HyDE、cross-encoder 重排与相关性门控
- **商用合规引擎**：广告法/医疗金融承诺/导流词表，发布前自动拦截违规内容
- **真实发布（已实测真发成功）**：Playwright 驱动创作者平台「上传图文」，攻克闭合 Shadow DOM 发布按钮、隐藏文件上传、平台改版容错；以 URL `published=true` 判定真成功
- **定时自动发布**：`content_bank` 稿件库 + 到期自动发 + `publish_log.json` 留痕 + 幂等防重复
- **可视化工作台**：统一 AI Studio 视觉语言的 Streamlit 6 页；状态页采用 Agent Console 信息布局，支持亮/暗主题
- **LangChain 生态实际接入**：助手工具走 `StructuredTool`；内容编排可切换 `StateGraph + RunnableLambda + SqliteSaver`
- **Docker 可复现运行**：固定 Python/核心依赖版本，Compose 一键启动；健康检查与命名卷保留会话、知识库和向量缓存
- **工程化**：三层解耦、跨层数据契约、Skill 注册路由、审计留痕、发布前自动质量门禁

## 🆕 PersonaX 2.3.1：滑动窗口与双重限流

本次是兼容 2.3 的小版本，不改变 RAG/Agent 主流程，主要补齐长对话和多会话同时运行时的资源边界：

| 优化 | 2.3 的问题 | 2.3.1 实现 | 原因 |
|---|---|---|---|
| 上下文滑动窗口 | 只固定取最近 8 条，单条超长消息仍可能挤占提示词 | 同时限制最近消息数、估算 token 和单消息 token；Checkpoint 仍保留较长历史 | 模型只接收当前需要的上下文，降低延迟、显存和费用，但不影响本地会话恢复 |
| LLM 请求频率 | 只有并发数限制，短时间连续启动请求仍可能触发 API RPM 限制 | 同步/异步入口共享 60 秒滑动窗口；每次真实请求及重试都计数 | 控制“单位时间启动多少次”，处理供应商速率配额 |
| LLM 在途并发 | 原有 Semaphore 只覆盖 async，Streamlit 同步流式未受保护 | 同步入口使用线程 Semaphore，异步入口使用 `asyncio.Semaphore`；Ollama 默认 1、云端默认 2 | 控制“同一时刻跑多少个”，避免单卡争抢显存或云端瞬时突发 |
| 联网增强限流 | 多会话可能同时消耗搜索额度 | 每后端默认 10 RPM、并发 2；忙时快速降级为空 | 搜索是可选增强，不能因配额排队阻塞正文主链路 |
| 真实发布单飞 | 发布只有次数配额，两个会话仍可能同时操作同一账号页面 | 发布频率继续独立滑动窗口，并增加进程内 `Semaphore(1)` | 浏览器状态和账号副作用必须串行，降低重复发布与风控风险 |
| 可观测窗口 | Trace 只记录固定的 `history=8` | 记录实际保留/总消息、估算 token、丢弃和截断数量 | 可以解释回答为什么没有携带更早历史 |

默认参数位于 `config/persona.yaml` 与 `.env.example`。完整适用范围、执行顺序及“不该限流”的模块见 [`docs/RATE_LIMITING_2_3_1.md`](docs/RATE_LIMITING_2_3_1.md)。

## 🆕 PersonaX 2.3：从“有评测”升级到“评测可信”

2.3 优先解决小样本得分虚高、不同向量模型共用阈值、无资料问题误召回和单次降级漏报：

| 优化 | 2.2 的限制 | 2.3 实现 | 验收收益 |
|---|---|---|---|
| 120 条 RAG 回归集 | 只有 6 条正例，容易得到偶然高分 | 108 条正例覆盖 27 份资料，另有 12 条知识库外负例；按主题与难度切片 | 同时衡量“找得到”和“不乱找” |
| 检索消融 | 只能看到混合检索总分 | 同一数据集一键比较 BM25、dense kNN、hybrid；输出 JSON/Markdown 报告 | 可以用数据说明为什么选择混合检索 |
| 双重召回指标 | 只看原始 Recall/MRR | 新增门控后 Recall、无答案拒检准确率和 P99；同一来源多 chunk 按来源去重 | 防止“召回分数高，但门控后资料全没了” |
| 后端阈值校准 | hashing 与 bge-m3 共用 `min_score=0.10` | hashing 使用 0.15，Ollama/sentence-transformers 使用 0.50，可按具体模型覆盖 | bge-m3 负例拒检从 0 提升到 1.0，正例门控 Recall@5 达到 0.9815 |
| 降级聚合 | 只检查最后一条查询的降级状态 | 聚合整个评测集所有组件故障及异常类型 | 前面样例发生的 embedding/reranker 故障不会被漏掉 |
| 回答证据检查 | 只检查有没有 `[1]` | 校验引用编号范围；无来源时强制标明“本地知识库未检索到可核验资料” | 减少无来源回答被误认为已有知识库支撑 |
| 建索引失败回退 | Ollama 建索引失败可能在 BM25 前直接中止 | 文档 embedding 失败时保留稀疏索引并记录 `dense_index` 降级 | Ollama 暂时不可用仍能完成关键词检索 |
| AI Studio 工作台 | 页面层级和按钮语言不统一，运行状态分散 | 统一六页导航、任务卡、工作流进度、Material 图标与亮/暗主题；状态页集中展示服务、门禁、运行和评测 | 日常操作更聚焦，排错信息更容易扫描 |

当前 120 条工程回归集实测：hashing hybrid 的 Recall@5 为 0.8889、门控 Recall@5 为 0.8056、MRR 为 0.7775、负例拒检为 0.75；本机 `bge-m3` 的 Recall@5 为 0.9537、校准门槛后的门控 Recall@5 为 0.9815、MRR 为 0.8035、负例拒检为 1.0。它仍是工程回归集，不等同于真实用户分布或线上 SLA；完整记录见 [`docs/RAG_EVAL_2_3.md`](docs/RAG_EVAL_2_3.md)。

### 流式对话与分层记忆

- `core.llm.complete_stream()` 对 Ollama 与 DeepSeek 使用 OpenAI 兼容接口的 `stream=True`，Streamlit 通过 `st.write_stream` 逐块显示；流中断后不会从头重试并重复输出。
- **短期记忆**：每个 thread 最多保存最近 20 条消息，提示词只取最近 8 条；页面重开后自动恢复最近会话，也可从“历史会话”切换。
- **长期记忆**：只保存用户在界面中主动确认的背景或偏好，每条最多 500 字，可随时查看和删除；默认最多注入最近 8 条。
- 会话记录与长期记忆均保存在本地 `logs/assistant_checkpoints.sqlite3`，不会自动从聊天中提取隐私，也不会作为指令执行。
- 点击停止时，未完成的半截回答不写入 Checkpoint；只有通过 Reviewer 的最终回答才会持久化。

### 异步 LLM 与 Semaphore 限流

- `core.llm.acomplete()` 是单请求异步入口，`acomplete_batch()` 并发执行相互独立的提示词，`acomplete_stream()` 则在整个流生命周期占用一个并发槽位。
- 本地 Ollama 默认 `LLM_MAX_CONCURRENCY=1`，云端默认 2；可在 `.env` 覆盖，但实现会把范围限制在 1～16。同一事件循环的所有异步批次共享 Semaphore，同步入口也共享独立的线程 Semaphore。
- 2.3.1 增加 `LLM_REQUESTS_PER_MINUTE` 滑动窗口。请求先等待频率额度，再占 Semaphore；因此等待 RPM 时不会空占稀缺并发槽位。
- 当前 Streamlit 对话仍是**同步流式**，但已受到同步 Semaphore 和同一个请求窗口保护。Supervisor → Retriever → Answerer → Reviewer 存在前后依赖，不会为了“看起来并发”而错误地同时启动。
- 8 个 250ms I/O 模拟请求中，Semaphore(2) 将总耗时从 2004.1ms 降到 1000.8ms（2.002x）；本机 Ollama `qwen2.5:3b` 的 4 请求实测则为 2277.0ms → 2279.6ms（0.999x），说明单 GPU 推理不应盲目提高请求并发。
- 运行 `python scripts/benchmark_async_llm.py --mode simulated --tasks 8 --concurrency 2 --delay-ms 250` 可复现基准；设计边界、流程与完整数据见 [`docs/ASYNC_ARCHITECTURE.md`](docs/ASYNC_ARCHITECTURE.md)。

## 🆕 PersonaX 2.2：这次具体优化了什么

2.2 的目标不是继续堆概念，而是解决 2.1 在“重启恢复、故障定位、质量回归、最终门禁”上的真实缺口：

| 优化 | 2.1 的问题 | 2.2 的实现 | 直接收益 |
|---|---|---|---|
| 持久化状态图 | `MemorySaver` 随进程退出丢失 | 官方 `SqliteSaver` 写入 `logs/langgraph_checkpoints.sqlite3`，保留最近 100 次运行 | 重启 Streamlit 后仍能查看 checkpoint 与运行轨迹 |
| 安全 checkpoint | 图状态含 Python 对象，默认反序列化边界偏宽 | 入库前转换为 JSON 兼容数据，`JsonPlusSerializer` 禁用 pickle 并启用严格类型白名单 | 降低本地 checkpoint 被篡改后的反序列化风险 |
| 节点级可观测性 | 只有总步数，难判断卡在哪一步 | 记录 Router、每个 Skill、风格检查、重试、状态、耗时、说明和 checkpoint ID | 可视化状态页可直接定位失败节点与重试原因 |
| 最终门禁修复 | 风格检查失败时可能残留前序 `publish_ready=true` | 最终风格门禁失败会覆盖为 `publish_ready=false`；CLI 在合规或就绪失败时停止发布 | 避免“图已拦截但后续仍执行 DryRun/发布”的逻辑漏洞 |
| RAG 质量门禁 | 评测只打印结果，没有通过阈值与退出码 | 固定 hashing 后端做可复现回归，检查 Recall@1、MRR、P50/P95 延迟和降级状态 | 可接 CI，质量下降会返回非零退出码 |
| 一键发布前验收 | 测试、合规、密钥、助手和 RAG 需分别执行 | `python scripts/release_check.py` 串联五道门禁，任一步失败立即停止 | 发布版本前只需一条命令，结果可复查 |

LangGraph 运行摘要只保存主题、状态、耗时、节点轨迹与 checkpoint 标识，不把完整提示词、正文或密钥复制到诊断表。真实发布安全锁保持默认关闭。

## PersonaX 2.1 基础：八个 Agent 模块的项目化落地

2.1 没有把所有概念堆成多个重型服务，而是围绕本地 3B/7B 模型做了一条可运行、可解释、可测试的对话链路：

```text
用户问题
  → Supervisor 路由（默认规则 / 可选 LLM JSON，失败回退）
  → LangChain StructuredTool → 只读工具网关（Schema + Harness + AuditLog）
  → Retriever Worker（BM25 + dense kNN + RRF + min_score）
  → Answerer Worker（一次 LLM 调用，结合最近 8 条消息）
  → Reviewer Worker（非空与引用检查）
  → SQLite Checkpoint + AuditLog + usage trace
```

| 教学模块 | PersonaX 2.2 实现 | 边界说明 |
|---|---|---|
| 1. Agent 认知与选型 | 采用显式、最多 4 步的 Agent loop；简单问候跳过检索 | 本地场景不为框架而框架 |
| 2. LLM 原语 | 所有生成统一走 `core.llm.complete()`；可选 Pydantic 结构化路由、JSON 解析与确定性回退 | `rule` 默认只调用一次 LLM；`llm` 路由会额外增加一次小调用 |
| 3. 状态机 | 助手有显式 Trace/Checkpoint；内容链提供真实 `StateGraph + SqliteSaver` 引擎 | 默认 Python 引擎保持低开销，CLI/UI 可显式切到 LangGraph |
| 4. Agentic RAG | 按需检索、混合召回、门控和回答引用；query enhancer / HyDE / dense / reranker 可独立降级 | dense 失败仍保留 BM25 + RRF，reranker 失败保留融合顺序 |
| 5. Multi-Agent | Supervisor + Retriever/Answerer/Reviewer 职责隔离 | 是可单测的逻辑 Worker，不冒充多个独立模型并行 |
| 6. MCP 与工具工程化 | `KnowledgeSearchTool` + `AssistantToolGateway` + LangChain `StructuredTool`：Schema、白名单、Harness 限流与审计 | 当前是 **transport-neutral MCP-ready 适配层**，尚未启动独立 MCP Server |
| 7. Eval & Observability | 12 条离线助手回归；路由、引用覆盖/有效性、无证据声明、步数和回答率；UI 展示逐步 Trace | 确定性评测不代表真实 LLM 语义质量，后续仍需人工/LLM-as-Judge 集合 |
| 8. 生产化 | 上下文裁剪、会话级限流、最大步数、组件级故障降级、本地 checkpoint、后端切换隔离、单实例 Docker 复现 | 未声称已完成高并发、Redis、多 Worker 或线上 SLA |

助手严格只读：它能查询知识库、解释项目和给出建议，但没有真实发布工具；发布仍必须经过现有人工确认与安全锁。

### LangChain / LangGraph 到底用在哪里

- `core/tool_gateway.py`：把只读知识检索导出为 LangChain Core `StructuredTool`，输入仍用 Pydantic Schema，执行仍必须通过 Harness。
- `core/graph.py`：用 LangGraph `StateGraph`、LangChain `RunnableLambda` 和 `SqliteSaver` 组成可执行的 Skill 状态图，包含风格校验、有界重试、严格序列化与运行留痕。
- CLI 使用 `python main.py generate --topic "..." --engine langgraph`；可视化生成页可选「LangGraph（状态图）」。
- 项目没有为了“用框架”强行改写 BM25/RRF 等已有可测试算法；LangChain 负责工具标准化和可组合执行，检索算法仍由 PersonaX 控制。

## 🏗️ 架构

```
接入层     main.py (CLI) + app.py (Streamlit 可视化)
   ↓
编排层     orchestrator.py（默认 Python Skill 链）+ assistant.py（对话 Supervisor-Worker）
           + graph.py（LangGraph StateGraph + LangChain Runnable，可选切换）
   ↓
能力层     skills/（标题/正文/标签/封面/就绪门禁）+ rag.py + llm.py（DeepSeek）
   ↓
基础设施   types.py(契约) registry.py(路由) harness.py(规则引擎) assistant_tools.py
           style.py
           compliance.py(合规) persona.py(多人格) prompts.py(提示词资产)
   ↓
执行层     publishers/（xhs.py Playwright 真发 / scheduler.py 定时调度）
```

## 🔎 RAG 混合检索改进

当前检索链路：

```text
用户主题
  → Query Rewriting（默认规则，可选 LLM）/ 可选 HyDE
  → BM25 关键词召回 + dense embedding 精确 kNN 召回
  → RRF 融合不同分数量纲的排名
  → 可选 Cross-Encoder 对候选文档重排
  → min_score 相关性门控
  → 相关知识注入内容生成提示词
```

主要改进：

- **BM25 稀疏检索**：保留专有名词、岗位名、学校名等精确关键词信号；零分文档不会进入 RRF。
- **dense kNN 语义检索**：用同一 embedding 模型编码查询和知识片段，遍历全部向量计算余弦相似度并取 Top-K。
- **RRF 排名融合**：只使用每路结果的名次进行融合，避免直接混合 BM25 分数与 cosine 分数。
- **查询增强**：短主题默认使用低延迟规则改写；复杂问题可通过现有 LLM 后端生成多查询或 HyDE 假设答案。
- **Cross-Encoder 重排**：只对 RRF 召回的少量候选做 query-document 联合打分，降低全库计算成本。
- **相关性门控**：`min_score` 过滤低相关知识；2.3 按 embedding 后端分别校准阈值，避免不同 cosine 分布共用一个数字。
- **可观测性**：每次召回将查询变体、embedding 后端、kNN 模式、融合与重排信息写入 `draft.metadata.rag_trace`。
- **组件级降级**：LLM query rewrite 失败回退规则改写；HyDE 失败跳过；dense 失败继续 BM25/RRF；reranker 失败保留 RRF 顺序；降级组件和异常类型写入 trace。
- **持久化嵌入缓存**：非 hashing 文档向量写入 D 盘 `.rag_cache/embeddings.sqlite3`，按模型和文本哈希复用；用户查询不入库，避免无界增长。
- **参考/范例分流**：公开百科标记为 `reference`，只用于概念和事实；手工收藏的优质笔记标记为 `example`，才用于学习结构与语气。

为什么现阶段使用精确 kNN，而不是 ANN：当前知识库规模较小，精确 kNN 实现直观、结果可复现且不会损失召回率。等知识库增长到数万以上 chunk，并通过性能测试确认向量遍历成为瓶颈后，再替换为 FAISS/HNSW 等 ANN 索引。

### Ollama 模型分工

内容生成模型和 embedding 模型用途不同，需要分别安装：

```powershell
# 内容生成、LLM Query Rewriting / HyDE
ollama pull qwen2.5:7b

# dense 语义检索
ollama pull bge-m3
```

### Windows 将 Ollama 模型放到 D 盘

模型默认位于 `%USERPROFILE%\.ollama\models`。C 盘空间紧张时，推荐先退出托盘中的 Ollama，再设置用户环境变量并重新启动：

```powershell
New-Item -ItemType Directory -Force D:\OllamaModels
setx OLLAMA_MODELS "D:\OllamaModels"
```

如果系统策略不允许写环境变量，也可在确认默认模型目录不存在后建立目录联接：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ollama"
New-Item -ItemType Junction `
  -Path "$env:USERPROFILE\.ollama\models" `
  -Target "D:\OllamaModels"
```

当前开发环境采用第二种方式：`%USERPROFILE%\.ollama\models` 只是指向 `D:\OllamaModels` 的联接，不在 C 盘重复保存模型。`bge-m3` 下载约 1.2GB，本机加载实测约占 664MB GPU 显存；实际占用会随 Ollama 版本、量化和运行设备变化。

`.env` 示例：

```env
LLM_BACKEND=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1

RAG_EMBEDDING_BACKEND=ollama
RAG_EMBEDDING_MODEL=bge-m3
RAG_EMBEDDING_CACHE=1
RAG_QUERY_ENHANCER=rule
RAG_ENABLE_HYDE=0
RAG_RERANKER=none
```

对话路由在 `config/persona.yaml` 中配置：

```yaml
assistant:
  router: rule  # rule（默认，低延迟）/ llm（结构化 JSON 路由，失败自动回退）
```

### 公开中文知识包（小体积）

项目提供定向导入器，从 Wikimedia 官方 API 获取武汉、校园、求职、Agent/RAG 和内容营销主题。每篇 Markdown 都保留原始链接、获取日期和许可信息，不会下载数 GB 的全量 dump。

```powershell
& .\.venv\Scripts\python.exe scripts\import_public_knowledge.py --embed
```

首次 `--embed` 会调用本地 `bge-m3`；之后只为新增或变更的 chunk 重算向量。删除 `.rag_cache/embeddings.sqlite3` 可完全重建缓存。

联网搜索与本地知识库互不改写：本地 RAG 提供稳定可复现的背景，联网搜索仅在生成当次补充时效信息。搜索摘要不会自动写入向量库。

默认 `hashing` embedding 仅用于无模型环境下的离线测试，不能称为真实语义向量模型。启用 Cross-Encoder 时需安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rag]"
```

检索回归评测与门禁：

```powershell
.\.venv\Scripts\python.exe main.py rag-eval --top-k 5 --backend hashing
# 本机 bge-m3 实测（需 Ollama 正在运行）
.\.venv\Scripts\python.exe main.py rag-eval --top-k 5 --backend ollama --min-score 0.50
# BM25 / dense / hybrid 消融，并保存可复查报告
.\.venv\Scripts\python.exe main.py rag-eval --compare --report-md docs/RAG_ABLATION.md
```

评测会输出 `Recall@K`、门控 Recall@K、`MRR`、负例拒检准确率、P50/P95/P99、分类/难度切片、阈值扫描和全量降级组件。默认使用 120 条工程回归样例与 hashing 后端接入发布门禁；真实语义质量另用本机 `bge-m3` 复测。后续对外描述线上效果前，仍应补充真实用户问题、人工相关性标注和 Cross-Encoder 对比。

## 🚀 快速开始

### 0. 环境准备（Python 3.10+）

```bash
git clone <你的仓库地址>
cd personax

# Windows 一键安装（自动装依赖 + Playwright + Chromium）
.\setup.ps1

# 或手动：
pip install -e .
pip install playwright
python -m playwright install chromium   # 不想下载可用系统浏览器 --browser msedge
```

### Docker 一键复现（推荐给体验者）

Docker 模式默认使用离线模板，因此没有 API Key、没有本地模型也能打开完整工作台：

```bash
git clone https://github.com/lin-or-feng/personax.git
cd personax
docker compose up -d --build --wait
```

打开 <http://localhost:8501>。Compose 默认只绑定 `127.0.0.1`；项目没有内置登录鉴权，不会默认向局域网或公网开放。查看状态与日志：

```bash
docker compose ps
docker compose logs -f app
```

要连接宿主机已有的 Ollama，先拉取模型，再在当前终端设置运行模式：

```powershell
ollama pull qwen2.5:7b
ollama pull bge-m3
$env:LLM_BACKEND = "ollama"
$env:RAG_EMBEDDING_BACKEND = "ollama"
docker compose up -d --build --wait
```

Compose 会把容器内的 Ollama 地址自动设为 `host.docker.internal:11434`；应用的连通性探测和实际调用使用同一地址。不要为了省事把 Ollama 的 11434 端口直接暴露到公网。

也可以走 DeepSeek：在项目根目录 `.env` 填入 `DEEPSEEK_API_KEY`，并设置 `LLM_BACKEND=deepseek` 后重新执行 `docker compose up -d --build --wait`。Key 只在容器启动时注入，`.dockerignore` 会阻止 `.env` 和浏览器登录态进入镜像。

`logs`、`.rag_cache`、`knowledge`、`content_bank` 和生成封面使用 Docker 命名卷；普通 `docker compose down` 后数据仍保留。`docker compose down -v` 会永久删除这些卷，只应在明确要重置全部容器数据时使用。

容器以非 root 用户和只读根文件系统运行，默认移除全部 Linux capabilities、启用 `no-new-privileges`、限制进程数并轮转日志。如确需局域网演示，可临时设置 `PERSONAX_BIND_HOST=0.0.0.0`，但必须同时配置防火墙和带认证的反向代理；不要直接做公网端口映射。漏洞报告方式见 [`SECURITY.md`](SECURITY.md)。

仓库中的 `.github/workflows/docker-smoke.yml` 会在 Docker 相关文件变化时构建镜像、等待容器健康并请求 Streamlit 健康端点，避免“配置文件存在但镜像实际起不来”。

> Docker 镜像用于复现生成、对话、RAG、评测和可视化链路。真实小红书发布固定关闭，也不把 Playwright 浏览器或 `storage_state.json` 打入镜像；有头发布调试继续在 Windows 宿主机运行。

### 1. 配置 DeepSeek Key（不配也能跑，走离线模板）

创建 `.env`（参考 `.env.example`，已被 .gitignore 忽略）：
```
DEEPSEEK_API_KEY=sk-你的key
```

### 2. 登录小红书（一次性，导出登录态）

```bash
python main.py login --browser msedge    # 弹浏览器扫码登录，生成 storage_state.json
```

### 3. 生成内容（干跑，不真发）

```bash
python main.py generate --topic "秋招穿搭"
python main.py generate --topic "考研英语" --persona 干货知识风   # 按人格生成
python main.py generate --topic "Agent 学习" --engine langgraph    # LangGraph 状态图，仍只干跑
```

### 4. 真实发布

```bash
python main.py publish --draft content_bank/example.json --real --browser msedge
```

发布前会人工确认（输入 `y`）；无人值守加 `--yes`。

### 5. 定时自动发布

```bash
# 稿件放 content_bank/*.json（scheduled_at 到期自动发）
python main.py schedule --run-once --real --yes --browser msedge
python main.py schedule --daemon --interval 60 --real --yes --browser msedge   # 常驻
```

### 6. 可视化工作台

```bash
pip install streamlit
python -m streamlit run app.py    # 打开 http://localhost:8501
```

Windows 也可以直接双击项目根目录的 `启动PersonaX可视化.cmd`，或使用桌面上的“PersonaX 可视化”快捷方式。入口会自动检查 Ollama、在独立后台进程中启动 Streamlit 并打开浏览器；命令窗口自动关闭不会停止页面，重复点击会复用已经运行的 8501 服务。启动失败时查看 `.tmp/start-ui.log` 和 `.tmp/streamlit-8501.stderr.log`。

进入左侧 **💬 AI 助手** 即可对话。默认使用本地 Ollama；可在页面上切换模型、开关本地知识库，并展开每条回答下方的“引用来源”和“Agent Trace”。助手不会调用发布功能。

### 7. 测试与评估

```bash
pytest tests/            # 124 项单测
python main.py eval      # 内容质量打分 → eval_results.csv
python -m eval.assistant_scorer  # AI 助手离线回归（不调用 Ollama）
python main.py rag-eval --top-k 1  # RAG Recall@K / MRR / 延迟门禁
python scripts/check_content_compliance.py  # 批量检查 content_bank 稿件
python scripts/release_check.py  # 单测 + 合规 + 密钥 + 助手 + RAG 一键门禁
python main.py notes --browser msedge   # 核实真实发布的笔记
```

## 📂 项目结构

```
personax/
├── main.py                 # CLI 入口（generate/publish/schedule/login/notes/probe/personas/eval/rag-eval）
├── app.py                  # Streamlit 可视化工作台
├── Dockerfile              # 非 root Streamlit 运行镜像 + 健康检查
├── compose.yaml            # 一键启动、宿主机 Ollama 接入与数据卷
├── requirements-docker.txt # 容器核心依赖版本基线
├── setup.ps1               # Windows 一键安装
├── config/
│   ├── persona.yaml        # 默认人格 + 系统规则（harness/rag/generation）
│   ├── personas.yaml       # 人格库（多人格）
│   ├── prompts.yaml        # 提示词资产（标题/正文/标签/封面模板）
│   ├── assistant_prompts.yaml # AI 助手提示词资产
│   └── compliance.yaml     # 合规词表（广告法/医疗金融/导流）
├── core/                   # 编排层 + 基础设施
│   ├── orchestrator.py     # 纯 Python 编排器
│   ├── assistant.py        # 对话 Supervisor-Worker + Checkpointer
│   ├── assistant_tools.py  # 类型化检索工具 + MCP-ready Manifest
│   ├── tool_gateway.py     # 只读工具白名单 + Schema + Harness + Audit
│   ├── graph.py            # LangGraph + SQLite checkpoint + 节点 Trace
│   ├── harness.py          # 规则引擎（滑动窗口/审批/审计）
│   ├── limits.py           # 线程安全滑动窗口限流器
│   ├── context_window.py   # 最近消息 + token 双重上下文窗口
│   ├── compliance.py       # 合规引擎
│   ├── rag.py              # RAG（BM25 + dense kNN + RRF + 可选重排）
│   ├── persona.py          # 多人格库
│   ├── prompts.py          # 提示词资产加载
│   ├── async_runtime.py    # Semaphore 有界并发 + 批处理指标
│   └── llm.py              # 同步/异步/流式 LLM 统一入口
├── skills/                 # Skill 系统（@register + 路由）
├── publishers/             # 发布层（Playwright 真发 / 定时调度）
├── knowledge/              # RAG 知识库（*.md 带 front-matter）
├── content_bank/           # 定时稿件库（*.json）
├── eval/                   # 评估闭环
└── tests/                  # pytest（124 项）
```

## 🎛️ 调优方向（怎么让内容更好）

| 杠杆 | 位置 | 说明 |
|---|---|---|
| ① 真实 LLM | `.env` 配 Key | 最大提升：模板文 → DeepSeek 现写 |
| ② 提示词资产 | `config/prompts.yaml` | 改生成要求，热更新不用改代码 |
| ③ 知识库 RAG | `knowledge/*.md` | 混合召回同主题资料；默认离线 hashing，Ollama `bge-m3` 才是真实语义向量 |
| ④ 多人格 | `config/personas.yaml` | 不同语气人设，`--persona` 切换 |
| ⑤ 发布稳定性 | `publishers/xhs.py` | 平台改版用 `python main.py probe --mode tuwen` 诊断 |

**发布稳定性诊断**（平台改版后元素找不到时）：
```bash
python main.py debug-selectors --browser msedge
python main.py probe --mode tuwen --upload assets/note_cover.png --browser msedge
```

## 🛡️ 安全与合规

- Key 存 `.env`（gitignore），登录态 `storage_state.json` 不提交
- **真实发布安全锁**：`XHS_REAL_PUBLISH_ENABLED=1` 才允许真发（默认关闭，仅生成/预览/干跑）
- 发布前人工审批（`--yes` 才跳过）+ 每分钟发布限速 + **幂等**防重复发
- **AI 声明强制校验**：发布前自动勾选「笔记含AI合成内容」并**回读确认已生效**，确认不到就**中止发布**（不发出未标识 AI 内容）
- 合规引擎自动拦截广告法绝对化用语 / 医疗金融承诺 / 导流话术
- 合规词表支持正则变体（如“最(好|强)”），无效自定义正则回退字面匹配，避免启动失败
- **对话助手只读**：工具清单仅包含本地知识检索，不暴露发布能力；检索片段作为不可信资料隔离注入提示词
- **会话级限流**：Streamlit 重跑时复用当前会话的 Harness，不会因每次点击重建对象而清空限额
- **外部链接白名单**：知识库来源仅保留 `http/https` 链接，其他协议不会在页面上生成可点击链接
- 审计留痕：`publish_log.json` + 运行期 AuditLog
- ⚠️ 自动化发布请遵守小红书平台规则，控制频率，谨慎使用

### 安全回归与验收

安全检查覆盖密钥/敏感文件泄漏、真实发布默认关闭、工具权限边界、会话限流、SQL 参数化、恶意知识片段隔离和外部链接协议校验。所有检查只使用本地模拟数据，**不登录、不调用小红书、不真实发布**。

```powershell
& .\.venv\Scripts\python.exe scripts\check_secrets.py --strict
& .\.venv\Scripts\python.exe scripts\check_content_compliance.py
& .\.venv\Scripts\python.exe -m pytest tests\test_security.py
& .\.venv\Scripts\python.exe -m pytest tests
# 或一键执行全部发布门禁：
& .\.venv\Scripts\python.exe scripts\release_check.py
```

本次完整的命令、环境、通过数和剩余边界记录在 [`docs/TEST_RESULTS.md`](docs/TEST_RESULTS.md)。

## 🔒 上传 GitHub 前安全校验

**每次 push 前跑一遍**，自动扫描密钥与敏感文件（.env / storage_state.json / 登录态 / Key 明文）：

```bash
python scripts/check_secrets.py             # 扫描；有 🔴 危险项会列出
python scripts/check_secrets.py --strict    # 发现危险项退出码 1（可挂 CI / pre-commit）
```

一键安装提交前钩子（每次 `git commit` 自动校验，危险项直接阻止提交）：

```bash
python scripts/install_hooks.py
```

> 校验规则与 `.gitignore` 同步：`.env`、`storage_state.json`、`logs/`、`publish_log.json` 等都会被识别为「已忽略、本地安全」，不会误报。
> ⚠️ **如果 Key 曾在任何渠道泄露过**（如聊天记录/截图），请到 [platform.deepseek.com](https://platform.deepseek.com) **删除重建**，新 Key 只放 `.env`。

## 💰 省钱方案（生成太花钱？）

每篇笔记会调 4 次 LLM（标题/正文/标签/封面），按性价比排序：

| 方案 | 成本 | 做法 | 质量 |
|---|---|---|---|
| **① 本地 Ollama（推荐）** | **0 元** | `.env` 加 `LLM_BACKEND=ollama`，先 `ollama pull qwen2.5:7b`；模型用 `LLM_MODEL=qwen2.5:7b` | 良好（本地 7B） |
| ② 云端便宜模型 | 极低 | 界面**别选 deepseek-reasoner**（贵 4-5 倍）；deepseek-chat 每篇约 1 分钱 | 好 |
| ③ 免费额度模型 | 0 元 | 智谱 GLM-4-Flash 免费：`.env` 设 `DEEPSEEK_BASE_URL` 换 OpenAI 兼容端点 | 中等 |
| ④ 省 token | — | 标题候选已 3 个；`generation.max_tokens` 可调小；已有内容的稿件不重复生成（调度器已做） | — |

> 已内置：`LLM_BACKEND=ollama` 自动走本地（无需 Key），默认 `qwen2.5:7b`（`LLM_MODEL` 可改），
> 输出上限自动降到 512 token；可视化界面模型下拉随后端切换。

## ⚠️ 已知限制（Roadmap）

- 话题「话题芯片」暂未自动添加（网页编辑器话题面板交互复杂，标签以 `#文本` 留正文，可在 App 补加）
- VectorStore 为内存实现（接口已抽象，可换 Milvus/Qdrant）
- SQLite checkpoint 适合本地单机与轻量部署，不适合多进程高并发；生产集群应换 PostgresSaver 等共享后端
- RAG/助手评测集已扩至 120/12 条，但仍是工程回归集，不代表真实用户分布或线上质量 SLA
- 单账号设计（多账号/并发为后续方向）
- Docker 面向单实例可复现演示；真实发布、有头浏览器和登录态仍在宿主机执行，不在容器中开放

## 🧰 技术栈

Python 3.10+ · DeepSeek (OpenAI SDK) · Playwright · Pydantic · Streamlit · LangChain Core · LangGraph · PyYAML · pytest

## 📄 License

MIT（请按需修改）
