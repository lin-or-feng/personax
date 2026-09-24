# PersonaX 验证记录

> 最后更新：2026-09-21（Asia/Shanghai）
> 验证边界：本地静态扫描、单元测试和离线评测；未登录或调用小红书，未触发真实发布。

## 阶段 1：安全基线

| 检查 | 命令 | 结果 |
|---|---|---|
| 密钥与敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 198 个文件，0 个可提交危险项 |
| 安全专项回归 | `python -m pytest tests/test_security.py` | 6 passed，0 failed |
| 完整单测 | `python -m pytest tests` | 79 passed，0 failed，0.92s |
| Python 语法检查 | `python -m py_compile app.py core/assistant.py core/assistant_tools.py` | 通过 |
| Git 差异格式 | `git diff --check` | 通过 |

安全专项覆盖：

- 来源 URL 仅接受 `http/https`，拒绝 `javascript/file/data` 与带内嵌凭据的链接。
- 知识库片段作为不可信 JSON 资料注入，无法闭合提示词边界。
- Streamlit 会话重跑时复用 Harness，限流计数不被重置。
- SQLite checkpoint 使用参数化查询，恶意 `thread_id` 不会改变表结构；损坏 JSON 安全回退为空历史。
- AI 助手工具清单仅有只读知识检索，不包含发布工具。

## 阶段 2：Agent 硬化与完整验收

| 检查 | 命令 | 结果 |
|---|---|---|
| Agent 硬化专项 | `python -m pytest tests/test_agent_hardening.py tests/test_assistant.py tests/test_security.py` | 23 passed，0 failed，0.19s |
| 完整单测 | `python -m pytest tests` | 87 passed，0 failed，1.08s（最终提交前重跑） |
| 助手离线评测 | `python -m eval.assistant_scorer` | 8/8 通过；路由准确率、引用覆盖率、步数受控率、回答率均为 1.0 |
| 本地向量检索 | `python -m eval.rag_scorer --top-k 1` | 6 条；Recall@1=1.0，MRR=1.0；`ollama:bge-m3` + exact kNN；156 cache hits / 0 misses |
| 本机 Ollama 端到端问答 | 单轮知识问题冒烟 | 8.61s；4 个来源，4 步；生成成功，Reviewer 补充缺失引用标记，因此 `degraded=true` |
| CLI 内容链路 | `LLM_BACKEND=offline python main.py generate --topic ...` | exit 0；生成、封面、合规、就绪门禁、DryRun 全链通过；`publish_ready=True` |
| Streamlit 脚本冒烟 | `streamlit.testing.v1.AppTest` | 0 个页面异常 |
| 运行中 UI 存活 | `GET http://127.0.0.1:8501/` | HTTP 200，响应 11141 bytes |
| Python 语法检查 | `python -m py_compile ...` | 通过 |
| 最终密钥扫描 | `python scripts/check_secrets.py --strict` | 通过；扫描 204 个文件，0 个可提交危险项 |

本轮增加的故障注入覆盖：LLM 路由非法 JSON、未知工具、工具参数越界、embedding 不可用、reranker 不可用、query enhancer/HyDE 不可用。相应结果均为受控阻断或降级，未进入发布链路。

## 阶段 3：PersonaX 2.1 LangChain / LangGraph / 合规验收

| 检查 | 命令 | 结果 |
|---|---|---|
| 完整单测 | `python -m pytest tests` | 92 passed，0 failed，1.45s（最终提交前重跑） |
| LangChain 工具路径 | 本机 Ollama 单轮问答 | Retriever Trace 包含 `runtime=langchain`；4 个来源，dense exact kNN，无检索降级 |
| LangGraph CLI | `LLM_BACKEND=offline python main.py generate --topic ... --engine langgraph --no-cover` | exit 0；真实执行 Skill/Harness/风格重试/就绪门禁；DryRun 成功，`publish_ready=True` |
| LangGraph 安全语义 | `tests/test_langgraph_runtime.py` | 图完成只记录 `graph_complete`，不伪造 `published`；敏感 Skill 仍写入审批审计 |
| 批量稿件合规 | `python scripts/check_content_compliance.py` | `content_bank` 3/3 通过，0 条违规 |
| 合规正则回归 | `tests/test_commercial.py` | “最好”、“第一推荐”可正确拦截；“最后”不误伤；无效正则 fail-safe 回退字面匹配 |
| Ollama 关闭降级 | `python -m eval.rag_scorer --top-k 1` | 11434 不可达时自动降级 BM25/RRF；6 条 Recall@1=0.8333，无崩溃 |
| Ollama 恢复复测 | 启动 Ollama 后重跑同一评测 | `ollama:bge-m3`；Recall@1=1.0，MRR=1.0；156 cache hits / 0 misses，无降级 |
| Streamlit 冒烟 | `streamlit.testing.v1.AppTest` | 0 个页面异常 |
| Python 语法 | `python -m py_compile ...` | 通过 |
| 密钥/敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 213 个文件，0 个可提交危险项 |

2.1 中“使用 LangChain”的边界：助手检索工具使用 LangChain Core `StructuredTool`；内容状态图使用 LangChain `RunnableLambda` 与 LangGraph `StateGraph/MemorySaver`。BM25、RRF、dense kNN 仍是项目内可测试实现，没有换成不透明的黑盒封装。

## 阶段 4：PersonaX 2.2 持久化工作流与质量门禁验收

| 检查 | 命令 | 结果 |
|---|---|---|
| 一键发布门禁 | `python scripts/release_check.py` | 五道门禁全部通过，12.62s |
| 完整单测 | `python -m pytest` | 97 passed，0 failed，2.59s（最终发布门禁复跑） |
| LangGraph SQLite 持久化 | `tests/test_langgraph_runtime.py` | 官方 `SqliteSaver` 成功保存并跨实例读取；checkpoint ID/数量与运行摘要完整 |
| Checkpoint 严格序列化 | 同上 + `core/graph.py` | 状态先转 JSON 兼容字典；禁用 pickle 与任意模块反序列化 |
| 最终风格门禁 | `test_final_style_gate_overrides_earlier_publish_ready` | 重试耗尽后覆盖 `publish_ready=false`，阻止残留就绪标记 |
| LangGraph CLI 故障冒烟 | `LLM_BACKEND=offline python main.py generate ... --engine langgraph --thread-id v22-smoke-gate --no-cover` | 风格句长超标后状态为 blocked；列出具体问题并停在 Publisher 前，未执行 DryRun |
| 可复现 RAG 门禁 | `python main.py rag-eval --backend hashing --top-k 1` | 6 条；Recall@1=0.8333，MRR=0.8333，P95=127.7ms，无降级，门槛 0.80 通过 |
| 本机语义向量实测 | `python main.py rag-eval --backend ollama --top-k 1` | `ollama:bge-m3`；Recall@1=1.0，MRR=1.0，P95=306.4ms；156 cache hits / 0 misses，无降级 |
| AI 助手离线回归 | `python -m eval.assistant_scorer` | 8/8 通过；路由、引用、步数、回答率均为 1.0 |
| 批量稿件合规 | `python scripts/check_content_compliance.py` | `content_bank` 3/3 通过，0 条违规 |
| Streamlit 页面冒烟 | `streamlit.testing.v1.AppTest` | 默认页与新增状态页均为 0 个页面异常 |
| Python 语法 | `python -m compileall -q app.py main.py core eval scripts` | 通过 |
| 密钥/敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 218 个文件，0 个可提交危险项 |

2.2 的可观测边界：运行索引保存主题、状态、节点、耗时、重试和 checkpoint 标识，不复制完整提示词、正文或密钥；真实 LangGraph state 由本地 SQLite checkpointer 管理并只保留最近 100 个 thread。

## 阶段 5：PersonaX 2.3 检索质量基线与证据门禁

| 检查 | 命令 | 结果 |
|---|---|---|
| 完整单测 | `python -m pytest` | 104 passed，0 failed，21.13s |
| 120 条 hashing 门禁 | `python -m eval.rag_scorer --summary-only` | Recall@5=0.8889；门控 Recall@5=0.8056；MRR=0.7775；负例拒检=0.75；无组件降级 |
| 检索消融 | `python main.py rag-eval --compare --backend hashing --summary-only` | BM25、dense、hybrid 使用同一数据集与门槛；仅 hybrid 全部门禁通过 |
| 本机 bge-m3 | `python main.py rag-eval --backend ollama --top-k 5 --min-score 0.50 --summary-only` | Recall@5=0.9537；门控 Recall@5=0.9815；MRR=0.8035；负例拒检=1.0；P95=388.5ms；156 cache hits / 0 misses |
| 助手离线回归 | `python -m eval.assistant_scorer` | 12/12 通过；路由、引用覆盖、引用编号有效性、无证据声明、步数和回答率均为 1.0 |
| 语法与差异格式 | `python -m compileall -q ...` + `git diff --check` | 通过 |

2.3 的 120 条数据是按当前 27 份知识资料构建的工程回归集，其中 108 条正例、12 条知识库外负例；它用于捕获版本回归，不代表真实用户分布。`min_score` 已按 embedding 后端拆分：hashing=0.15，Ollama/sentence-transformers=0.50；替换模型后必须重新做阈值扫描。

## 阶段 6：AI Studio 界面验收

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| Streamlit 配置 | `python -m streamlit config show` | 亮/暗主题、侧栏主题与最小工具栏配置均成功解析 |
| 六页浏览器巡检 | Edge + Playwright 依次打开生成、助手、定时、知识库、设置、状态页 | 6/6 页面标题到达，`stException` 均为 0；截图与结构化结果保存至 `artifacts/ui_review/` |
| 导航回归 | 从内容库“载入编辑”返回生成页 | 旧文案型导航值已迁移为稳定页面 ID `generate` |
| Python 语法 | `python -m py_compile app.py` | 通过 |
| 完整单测 | `python -m pytest` | 104 passed，0 failed，23.70s |
| 密钥与敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 250 个文件，0 个可提交危险项 |
| 差异格式 | `git diff --check` | 通过 |

本轮只巡检本地界面与已有离线测试，没有触发 LLM 内容生成、平台登录或真实发布。

## 阶段 7：流式对话与分层记忆

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 流式回答单测 | `test_streaming_reply_persists_completed_answer` | 分块输出按顺序合并，完成后保存最终回答与 `mode=stream` Trace |
| 本机 Ollama 流式冒烟 | `qwen2.5:3b`，`stream=True`，24 token 上限 | 6.78s 返回 4 个内容块，最终文本完整 |
| 离线流式降级 | `test_llm_stream_has_offline_fallback` | 无云端 Key、无模型调用时仍走统一流接口并返回可显示文本 |
| 会话恢复 | `test_checkpoint_lists_recent_threads` | 最近会话按更新时间列出，包含消息数与安全截断预览 |
| 长期记忆 CRUD | `test_long_term_memory_is_explicit_and_deletable` | 主动保存、重复去重、读取和删除均通过 |
| 记忆注入边界 | `test_saved_memory_cannot_close_context_boundary` | HTML 边界字符被 JSON 转义，记忆无法闭合不可信数据区或覆盖系统指令 |
| 六页浏览器巡检 | Edge + Playwright | 6/6 页面可达，`stException` 均为 0；助手页成功恢复本地历史并显示记忆控件 |
| 完整单测 | `python -m pytest` | 110 passed，0 failed，20.09s |
| 密钥与敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 252 个文件，0 个可提交危险项 |
| Python 语法与差异格式 | `python -m py_compile ...` + `git diff --check` | 通过 |

验证没有登录或调用小红书，也没有触发真实发布。浏览器巡检没有主动发送 LLM 请求；模型流由确定性分块单测覆盖。

## 阶段 8：有界异步 LLM 与并发压测

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| Semaphore 行为 | `tests/test_async_runtime.py` | 保持输入顺序；并发峰值不超过限制；部分失败可独立统计；两个并行批次共享同一 API 并发门 |
| 离线异步契约 | `acomplete_batch()` + `acomplete_stream()` | 批处理、异步流和离线降级均通过统一公共入口 |
| I/O 模拟基准 | 8 任务 × 250ms，Semaphore(2) | 2004.1ms → 1000.8ms；3.992 → 7.994 req/s；2.002x |
| 本机 Ollama 基准 | `qwen2.5:3b`，4 个约 64 token 请求，Semaphore(2) | 2277.0ms → 2279.6ms；1.757 → 1.755 req/s；0.999x；0 失败 |
| 本机并发 4 复测 | 同上，Semaphore(4) | 2277.1ms → 2284.8ms；0.997x；提高并发没有加速，因此本地默认 1 |
| 完整单测 | `python -m pytest` | 114 passed，0 failed，19.20s |
| Python 语法与差异格式 | `python -m py_compile ...` + `git diff --check` | 通过 |

异步只用于相互独立、以等待为主的任务；依赖型 Agent 链、Playwright 发布、cross-encoder 和单卡模型计算保持串行或批处理。原始报告与复现命令见 `docs/ASYNC_ARCHITECTURE.md`，本轮没有登录或调用小红书，也没有触发真实发布。

## 阶段 9：PersonaX 2.3.1 滑动窗口与双重限流

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 上下文滑动窗口 | `tests/test_limits.py` + `tests/test_assistant.py` | 最近消息优先；消息数、估算 token 和单消息预算均生效；Trace 记录保留/丢弃/截断信息 |
| 精确请求窗口 | `SlidingWindowRateLimiter` 注入单调时钟测试 | 窗口按单个事件时间过期，不受整分钟边界影响；同步/异步等待路径均通过 |
| 同步 LLM Semaphore | 3 线程并发调用，Semaphore(1) | 峰值真实调用数为 1，Streamlit/CLI 同步路径不再绕过并发门 |
| 异步跨批次 Semaphore | 两个批次同时运行，Semaphore(2) | 6 个任务完成，合计真实调用峰值为 2 |
| 联网搜索双门 | 1 RPM 故障注入 | 第一次返回结果，第二次安全降级为空；正文主链路不阻塞 |
| 真实发布单飞 | 预占发布 Semaphore 后调用 Publisher | 第二个真实发布立即受控拒绝，未启动浏览器、未消耗平台操作 |
| 完整单测 | `python -m pytest` | 121 passed，0 failed，19.61s |
| 一键发布门禁 | `python scripts/release_check.py` | 单测、3/3 稿件合规、密钥、12 条助手回归、120 条 RAG 门禁全部通过，38.72s |
| 密钥与敏感文件 | `python scripts/check_secrets.py --strict` | 扫描 272 个文件，0 个可提交危险项 |
| Python 语法与差异格式 | `python -m compileall -q ...` + `git diff --check` | 通过 |

验证只使用离线/确定性故障注入，没有登录或调用小红书，没有触发真实发布。限流器是单进程实现；多 Worker 部署仍需 Redis 等共享计数器与分布式锁。

## 阶段 10：Docker 可复现运行

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| Compose 结构 | PyYAML 解析 `compose.yaml` + 契约测试 | 通过；默认 offline，8501 仅绑定 `127.0.0.1`，宿主机 Ollama 地址和 5 个持久化卷有效 |
| 容器 Ollama 地址 | `tests/test_docker_deployment.py` 注入 `host.docker.internal:11434` | TCP 探测使用配置端点，不再写死容器自身 `127.0.0.1` |
| 构建上下文安全 | `.dockerignore` 契约测试 + `check_secrets.py --strict` | `.env`、登录态、日志和缓存不进入镜像；281 个文件中 0 个可提交危险项 |
| Docker 专项回归 | `pytest tests/test_docker_deployment.py tests/test_async_runtime.py` | 8 passed，0 failed，0.27s |
| 完整单测 | `python -m pytest tests` | 124 passed，0 failed，21.04s |
| Python/YAML/差异格式 | `compileall` + PyYAML + `git diff --check` | 通过 |
| 镜像构建与健康检查 | `docker compose up -d --build --wait --wait-timeout 300` | Windows 10 + WSL 2.7.14 + Docker Engine 29.8.0 实机通过；`personax:2.3.1` 构建成功，容器状态 healthy |
| HTTP 与运行身份 | 请求 `http://127.0.0.1:8501/`、`/_stcore/health` + 容器内 `id` | 页面与健康端点均返回 HTTP 200（health=`ok`）；进程以 UID 100 `personax` 非 root 用户运行 |
| 依赖与数据位置 | 容器内版本检查 + 宿主机数据盘检查 | Streamlit 1.62.0、LangChain Core 1.6.0；Docker VHDX 位于 `D:\DockerData`，未占用 C 盘作为主数据盘 |
| 宿主机 Ollama 集成 | 容器内 `core.llm.complete` + `OllamaEmbedder.encode` | `host.docker.internal` 链路通过；qwen2.5:7b 最小生成返回 `OK`，bge-m3 返回 1024 维归一化向量；测试后卸载驻留模型 |
| 容器运行时加固 | `docker inspect` + 根文件系统/命名卷写入探针 | `ReadonlyRootfs=true`、PID=256、`CapDrop=ALL`、`no-new-privileges`；根文件系统写入被拒绝、命名卷可写，端口仅绑定 `127.0.0.1` |
| 镜像敏感物检查 | 容器内检查 `.env`、`storage_state.json`、`publish_log.json`、`.git` | 全部不存在；本地密钥、浏览器登录态、发布日志和 Git 历史未进入镜像 |
| 持续集成 | `.github/workflows/docker-smoke.yml` | GitHub Actions `Docker smoke test #1` 对提交 `8138164` 实际通过，完成镜像构建、容器健康等待和 HTTP smoke test，耗时 58s（run `35845614959`） |

Docker 默认只复现生成、对话、RAG、评测和 Streamlit 工作台，真实发布安全锁固定关闭，不包含浏览器登录态。本轮已完成当前 Windows 主机的真实镜像构建与健康检查，但不能替代第二台机器的跨机验收。

## 阶段 11：PersonaX 2.4 持久化 HITL 与安全运行时

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 跨重启审批 | `test_publish_approval_survives_restart_invalidates_edits_and_is_consumed` | LangGraph interrupt 写入 SQLite；新 Store 实例可 resume；草稿变更被拒绝；发布成功转为 consumed |
| 审批缺失阻断 | `test_real_publish_rejects_missing_persisted_approval` | 开启严格模式但缺少审批时，在登录态和浏览器操作前抛出受控拒绝 |
| Agent 四重预算 | `test_agent_budget_blocks_loops_steps_and_token_overflow` | 重复状态循环与估算 Token 超限均被中止；步骤/总超时使用同一预算对象 |
| 工具重试与输出预算 | `test_tool_gateway_retries_transient_error_and_bounds_output` | ConnectionError 第 3 次成功；attempts、trace_id 完整；输出严格裁剪至 500 字符 |
| 确定性安全红队 | `python -m eval.security_scorer --summary-only` | 16/16 通过：知识/记忆边界、危险 URL、未知工具、PII 共 5 类 |
| Streamlit 冒烟 | `streamlit.testing.v1.AppTest` | 0 个页面异常 |

审批与发布测试只替换了最终平台动作，不启动 Playwright、不登录、不真实发布。真实发布后端会重新校验审批，不信任前端 Session State。

## 阶段 12：PersonaX 2.5 父子检索与记忆治理

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 父子 Chunk 回归 | 120 条 hashing hybrid，Top-5 | Recall@5=0.8889；门控 Recall@5=0.8056；MRR=0.7775；负例拒检=0.75；无降级；门禁通过 |
| 旧切块对照 | 同参数加 `--flat-chunks` | 四项质量指标与父子 Chunk 完全一致；两次单跑的延迟仅作冒烟，不用于性能结论 |
| 版本与上下文 | `test_heading_parent_child_chunks_keep_versions_and_expand_context` | 子块保留文档/索引版本和父段元数据；命中后可展开完整父段 |
| 索引失败恢复 | `test_failed_index_is_not_cached_and_can_recover` | embedding 构建失败实例不缓存；服务恢复后相同知识目录可重建成功 |
| 记忆治理 | TTL/PII/摘要候选/导出/删除测试 | 常见 PII 被掩码，过期记忆不返回；摘要只生成候选；导出和删除全部通过 |
| 完整发布门禁 | `python scripts/release_check.py` | 134 单测、3/3 稿件合规、298 文件密钥扫描、16/16 红队、12/12 助手、120 条 RAG 全部通过，58.41s |
| CI 覆盖率命令 | `pytest --cov=core --cov=publishers --cov-fail-under=55` | 133 passed；总覆盖率 68.76%，超过 55% 门槛；新增消融逻辑测试后总数为 134 |
| Python 与差异格式 | `compileall` + `git diff --check` | 通过 |

Cross-Encoder 消融入口已经实现，但本轮没有下载新的重排模型，因此不记录或冒充 Cross-Encoder 效果。待用户选择模型并确认磁盘预算后，应运行 `main.py rag-eval --compare-reranker` 生成独立报告。

## 阶段 13：PersonaX 2.6 可信回答与端到端可观测

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 答案支持度固定集 | `python -m eval.grounding_scorer --summary-only` | 16 条通过；Accuracy=1.0，unsupported recall=1.0，阈值分别为 0.90/0.85 |
| 助手端到端回归 | `python -m eval.assistant_scorer` | 12/12 通过；路由、引用覆盖/有效性、词法支持度、无证据声明、步数与回答率均为 1.0；仅预期的无证据样例降级 |
| Trace 贯通 | `test_tool_and_llm_telemetry_share_trace_id` / `test_assistant_passes_trace_id_to_router_and_answer_model` | 路由/回答 LLM、工具调用与回复共用同一 Trace；遥测不含 query、提示词、正文和工具参数 |
| 遥测聚合 | `test_observability_aggregates_quality_latency_tokens_and_cost` | 工具成功率、助手降级率、Token、可配置成本及 P95 口径通过；损坏/超长行安全跳过 |
| Streamlit 状态页 | `streamlit.testing.v1.AppTest`，`nav=observability` | 0 个页面异常；可显示安全聚合、指定 Trace 与最近事件 |
| CLI 冒烟 | `python main.py telemetry --limit 10` | 正常输出结构化聚合；未配置单价时 `pricing_configured=false` |
| 完整发布门禁 | `python scripts/release_check.py` | 七道门禁全部通过：143 单测、3/3 稿件合规、309 文件密钥扫描、16/16 红队、16 条支持度、12/12 助手、120 条 RAG；40.83s |
| 覆盖率门禁 | `pytest --cov=core --cov=publishers --cov-fail-under=55` | 143 passed；总覆盖率 70.03%，超过 55% 下限；`grounding.py` 99%，`observability.py` 94% |

本阶段所有测试均为本地只读或模拟验证；没有登录平台、启动 Playwright 或执行真实发布。Token 成本来自用户配置单价的估算，不是供应商账单。

## 阶段 14：PersonaX 2.7 真实反馈与排序实验闭环

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 反馈最小化与幂等 | `test_feedback_is_idempotent_per_trace_and_exports_only_opted_in_failures` | 同一 Trace 更新而不重复插入；默认不导出正文；显式同意的负反馈进入候选 |
| 脱敏与安全扫描 | 手机号、邮箱、`sk-` 测试夹具 + `check_secrets.py --strict` | 写入前完成 PII/凭据掩码；首次门禁发现源码中的完整假 Key 后改为运行时拼接，最终 316 文件扫描无可提交危险项 |
| 反馈聚合与删除 | `test_feedback_summary_does_not_require_stored_conversation_content` | 有帮助率、原因计数、候选数正确；全部反馈可删除且清零 |
| NDCG 单元回归 | `test_ndcg_rewards_all_relevant_sources_and_ranking_order` | 多个正确来源全部前置时 NDCG=1.0；延迟排序按折损位置得到 0.6934 |
| RAG 120 条基线 | hashing hybrid，Top-5，`min_score=0.15` | Recall@5=0.8889；门控 Recall@5=0.8056；MRR=0.7775；NDCG@5=0.8049；门控 NDCG@5=0.7710；拒检=0.75 |
| Streamlit 页面 | `AppTest`：状态页 + 注入带 Trace 的助手回答 | 两页均 0 异常；助手页面检测到 1 个原生 feedback 控件 |
| CLI 冒烟 | `python main.py feedback` | 无反馈时输出结构化空聚合和候选数组，不创建可提交数据文件 |
| 完整发布门禁 | `python scripts/release_check.py` | 七道门禁全部通过：147 单测、3/3 稿件合规、316 文件密钥扫描、16/16 红队、16 条支持度、12/12 助手、120 条 RAG；42.84s |
| 覆盖率门禁 | `pytest --cov=core --cov=publishers --cov-fail-under=55` | 147 passed；总覆盖率 70.68%，`core/feedback.py` 89%，超过 55% 下限 |

本轮没有安装或下载 Cross-Encoder，因此只记录 RRF 基线新增的 NDCG，不声称重排模型带来提升。所有反馈测试使用临时 SQLite；没有登录平台、启动 Playwright 或真实发布。

## 阶段 15：PersonaX 2.8 FastAPI / SSE 服务边界

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| API 专项与相关回归 | `pytest tests/test_api.py tests/test_assistant.py tests/test_docker_deployment.py` | 30 passed；覆盖 health/ready、JSON、SSE、Bearer、API 全局 RPM、输入上限、错误脱敏、无发布路由、RAG 并发单例和 Compose 安全默认值 |
| 完整单元测试 | `pytest` | 156 passed，0 failed，21.00s（发布门禁内） |
| 依赖与语法 | `pip check` + `compileall api.py core tests` | 无损坏依赖；Python 编译通过 |
| 原生 Uvicorn 冒烟 | `uvicorn api:app --host 127.0.0.1 --port 8108 --workers 1`，离线 LLM | health=`ok`、ready=`ready`、JSON HTTP 200、SSE HTTP 200，包含 token/done 与 X-Trace-ID |
| Compose 静态校验 | 默认和 `--profile api` 的 `docker compose config --quiet` | 两种配置均通过；API 默认绑定本机且单 Worker |
| Docker 镜像与健康 | `docker compose --profile api up -d --build api --wait`，宿主端口 8109 | 最终代码状态的 `personax:2.8.0` 构建成功；容器 healthy；JSON/SSE 均 200，配置 2 RPM 时第三次请求为 429、`Retry-After=60` |
| 容器身份与加固 | `id` + `docker inspect personax-api-1` | UID 100 非 root；`Restart=no`、`ReadonlyRootfs=true`、PID=256、`CapDrop=[ALL]`、`no-new-privileges=true` |
| 完整发布门禁 | `python scripts/release_check.py` | 七道门禁全部通过：156 单测、3/3 稿件合规、16/16 红队、16 条支持度、12/12 助手、120 条 RAG；46.52s；最终密钥复扫 324 文件无可提交危险项 |
| 覆盖率门禁 | `pytest --cov=core --cov=publishers --cov=api --cov-fail-under=55` | 156 passed；总覆盖率 71.19%，`api.py` 84%，超过 55% 下限 |

容器验证显式设置 `LLM_BACKEND=offline` 且清空云端 Key，没有调用云端模型、没有登录平台、没有真实发布。验证结束后已停止并删除 API 测试容器，Docker Desktop 已通过官方 CLI 停止；镜像和命名卷保留。FastAPI 目前是本机/受控局域网单 Worker 服务，不等同于公网生产部署或线上 SLA。

## 阶段 16：PersonaX 2.8.1 双语 API 文档

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| API 文档专项 | `pytest tests/test_api.py -q` | 12 passed；覆盖根路径与 `/docs` 默认简中跳转、简中/英文页面、双语 OpenAPI、未知语言 404、favicon 与无发布路由 |
| 技术契约 | 对比 `/openapi/zh-CN.json` 与 `/openapi/en.json` | 路径、字段名与 SSE 事件名保持英文；接口用途、参数说明和示例按语言切换 |
| 完整单元测试 | `pytest -q` | 159 passed，0 failed |
| 完整发布门禁 | `python scripts/release_check.py` | 七道门禁全部通过：159 单测、3/3 稿件合规、327 文件敏感信息扫描、16/16 红队、16 条支持度、12/12 助手、120 条 RAG；44.28s |
| 安全边界 | OpenAPI 路径断言 + 代码审查 | 文档仍不暴露 publish 路径；未加入隐藏启动、自启动、注册表项或计划任务 |

本阶段只修改 API 文档展示、OpenAPI 元数据与请求示例，没有调用模型、启动浏览器自动化、登录平台或真实发布。Docker 镜像没有在本阶段重新构建，2.8.0 的容器验收记录保持为历史证据。

## 剩余风险与不声称项

- 静态扫描能降低误提交凭据的风险，不等于专业 SAST/DAST 或依赖供应链审计。
- 本轮未用受限账号做平台端到端发布测试，这是刻意的安全边界，不代表发布流程已在当前平台状态下验收。
- 本地 `.env`、登录态和发布日志存在但已被 `.gitignore` 忽略；它们不在本次提交范围。
- RAG 120 条、助手 12 条仍是围绕当前知识库构建的工程回归集，不可对外宣称为真实用户质量基准或线上 SLA。
- Reviewer 已能捕获引用编号错误、低词法重合与明显否定冲突，但不能证明复杂同义改写、数值推理或跨句蕴含正确；仍需真实问题人工集、NLI 或经校准的 Judge。
- 用户反馈候选仍可能包含上下文敏感信息；自动掩码只是降低风险，进入正式评测集前仍必须人工复核与去重。
