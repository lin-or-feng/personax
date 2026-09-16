# PersonaX 验证记录

> 最后更新：2026-09-16（Asia/Shanghai）  
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

## 剩余风险与不声称项

- 静态扫描能降低误提交凭据的风险，不等于专业 SAST/DAST 或依赖供应链审计。
- 本轮未用受限账号做平台端到端发布测试，这是刻意的安全边界，不代表发布流程已在当前平台状态下验收。
- 本地 `.env`、登录态和发布日志存在但已被 `.gitignore` 忽略；它们不在本次提交范围。
- RAG 6 条、助手 8 条均是小样本冒烟集，不可对外宣称为大规模质量基准或线上 SLA。
- 本机 Ollama 问答暴露了一个可观测信号：当前本地模型可能遗漏行内引用，Reviewer 会补充 `[1]` 并标记降级；后续应用更大人工集验证引用是否准确对应到具体句子。
