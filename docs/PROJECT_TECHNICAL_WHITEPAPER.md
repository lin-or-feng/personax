# PersonaX 架构说明

更新：2026-10-22。发布基线 2.8.1；目录库和动态检索 Agent 尚未发布。操作命令在 [README](../README.md)，测量记录在 [TEST_RESULTS](TEST_RESULTS.md)。

## 1. 需求与适用范围

项目从内容生成与发布脚本开始，逐步补充知识引用、审核、审批和执行核验。主要问题是生成格式不稳定、资料跑题、误报发布成功，以及失败后缺少可追溯记录。

当前面向个人内容处理、本地资料问答和单实例服务集成。系统将两类控制分开：

- 发布 Workflow：步骤及审批条件由代码确定，模型不能跳过安全锁。
- 检索 Agent：根据本轮工具观察选择查询、补查、澄清或停止，只具备本地只读检索能力。

模型输出计划文字或使用框架名称并不等于动态决策。实际循环位于 `core/retrieval_agent.py`；发布链路仍由内容编排和 Publisher 控制。

## 2. 分层与模块

| 层 | 模块 | 责任 |
|---|---|---|
| 接入 | main.py、app.py、api.py | CLI、工作台、JSON/SSE，输入校验和服务边界 |
| 编排 | orchestrator.py、graph.py、assistant.py、retrieval_agent.py | 内容步骤、Agent 循环、预算、回答检查 |
| 能力 | skills/、rag.py、assistant_tools.py、document_parser.py、llm.py | 生成、检索、受限解析、统一模型调用 |
| 数据与管控 | types.py、harness.py、tool_gateway.py、knowledge_store.py | 契约、限流、工具权限、SQLite 快照 |
| 副作用 | publishers/ | 浏览器动作、调度和执行结果，不进入只读助手工具清单 |

内容编排可选择普通 Python 或 LangGraph。LangGraph 路径使用 StateGraph、RunnableLambda 和 SqliteSaver；助手工具通过 LangChain Core StructuredTool 标准化。检索算法由项目实现，Agent 当前采用异步 Python 循环，而非另一个 StateGraph。

模块依赖与开发要求见 [SPEC](../SPEC.md) 和 [AGENTS](../AGENTS.md)。可视化图见 [架构与流程图](知识图谱.md)。

## 3. 核心契约

- `Draft / SkillInput / SkillOutput / ExecutionContext`：传递草稿与能力调用上下文。
- `AssistantRequest / AssistantResponse`：统一问答入口，带来源、执行轨迹、预算统计和停止码。
- `RetrievalAgentDecision`：仅允许 search、finish、clarify、insufficient_evidence；拒绝额外字段。
- `ToolCallRequest / ToolCallResult / KnowledgeSearchInput`：工具边界与错误分类。
- 文档、Chunk、版本与任务契约：明确当前有效快照及引用位置。

客户端不能传入服务端数据库路径、向量存储地址或伪造 trace_id。模型不能指定工具名称、用户身份、库权限、top_k 或 min_score。

## 4. 内容与发布

内容步骤按依赖执行：选择人格和 Skill → 生成 → 风格与合规检查 → 就绪门禁 → 经授权的发布入口 → 核验结果。

YAML 保存人格与提示词；风格规则限制禁词、句长和格式。合规检查针对绝对化用语、医疗金融承诺及导流话术，不代替法律或平台审核。AI 声明属于发布条件，不因清理文档而移除。

真实发布默认关闭。Playwright 只在显式启用后使用本机登录态；容器不安装发布浏览器，也不接管宿主机 Cookie。操作失败保留调试现场，不能仅因点击按钮就报告成功。

### 审批与幂等的限制

持久化审批入口使用 LangGraph interrupt 和 SQLite checkpoint。审批绑定用户与草稿指纹，草稿变化使原批准不再匹配；审批消费状态用于防止重复提交。

基础 CLI 的交互确认与持久化审批入口并非完全相同。草稿指纹包含封面路径而不是图像字节；当前没有审批 TTL，也没有覆盖所有崩溃/并发情况的 exactly-once 保证。扩展发布入口必须逐项核对，不得推断所有入口已有相同保障。

## 5. 检索与技术选择

当前链路：可选查询增强 → BM25 与 dense 精确 kNN 双路召回 → RRF → 可选 Cross-Encoder → 相关性门控 → 来源去重与引用。

| 选择 | 原因 | 何时重新评估 |
|---|---|---|
| BM25 | 保留标识符、专有名词等精确词项信号，计算成本低 | 语料语言、分词或词项分布变化时重测 |
| 稠密向量 | 补充表达差异和语义相近内容 | 更换模型、语料或语言后重标门槛 |
| RRF | 以排名融合，避免直接相加不同量纲的分数 | 有足够标注数据时评估学习排序或加权融合 |
| 精确 kNN | 当前小型库便于验证，不引入近似召回损失 | 向量遍历成为实测瓶颈后比较 ANN |
| SQLite | 单机部署、事务和备份简单，无需外部服务 | 多实例写入、共享配额或权限模型超出单机能力时 |
| Qdrant 本地模式 | 可选持久化适配，验证同一检索接口 | 服务端部署和 ANN 性能需要单独测试 |

hashing 是离线特征后端，不是真实语义模型。bge-m3 用于语义向量；Qwen 用于内容和行动决策。LLM query rewriting、HyDE 和 Cross-Encoder 均可选，不表示每条目录库查询默认执行。

RRF 召回和门控使用不同口径。当前来源级 Recall 实际统计“预期来源是否进入结果”；门控先过滤候选再取 Top-K，不能把门控前后差值直接称为模型质量提升。历史数据、阈值与消融见 [RAG 记录](RAG_EVAL_2_3.md)。

## 6. 文档治理与实体关联

导入仅允许操作者配置根目录中的相对路径，拒绝越界、软硬链接以及超出资源上限的文件。MD/TXT/HTML 处理不执行脚本；PDF 只做文本提取，不提供 OCR 或进程级恶意文件沙箱。

稳定文档 ID 与内容/配置版本分开。文档、Chunk 和完成状态在 SQLite 事务内提交，以预期版本和单调 revision 拒绝过期写入；解析失败保留旧版本。

新查询只使用当前有效快照。软删除不删除原文件、不擦除历史字节，也不追溯撤销已开始的快照。Qdrant 查询按库与有效 point ID 过滤；旧向量物理清理由独立策略处理。

实体关系目前为原文 `co_mentions`，每条边绑定文档版本和 Chunk 证据，查询最多两跳并排除失效关系。这是来源支持的共现图，不是因果图谱或完整 GraphRAG。

导入、删除、恢复及操作限制见 [目录库](KNOWLEDGE_BASE.md)。

## 7. 助手、记忆与输出

默认 Workflow 运行固定检索/回答路径。Agent 每次将目标和工具观察送入决策模型，校验返回动作，经受控工具执行，再观察结果。

最多三次检索；重复查询停止。有证据后检索无新增内容时，允许一次收尾决策，但不再执行搜索。超时、权限失败和预算耗尽不能触发无限重试。

无证据不生成事实结论。跨文档比较要求至少两个独立来源；生成内容未通过引用/词法支持检查时整段回退摘录。该检查可能误拒同义改写，也不能证明语义正确。

历史按消息数和估算 Token 双重裁剪。SQLite checkpoint 保存完成的对话，不保存 Agent 中途状态。长期记忆只接收用户显式保存/确认的内容，支持脱敏、TTL 和删除；不自动从对话提取隐私。

反馈默认保存评分、Trace、哈希和计数。用户显式同意后才保存有限脱敏摘要；导出的候选须人工标注、去重与分集，不能直接视作评测真值。哈希不是加密或匿名化保证。

协议和故障码见 [Agent 说明](RETRIEVAL_AGENT.md)。

## 8. 异步、限流与取消

决策与模型生成使用异步入口。检索中的同步工作放入线程并等待安全收尾；不能声称 Python 线程可强制取消。

请求先等 RPM 滑动窗口额度，再占 Semaphore 在途槽；本地默认并发 1。同步和异步入口使用各自的并发门，异步门按事件循环共享；它们不构成跨进程或跨所有入口的统一并发上限。RPM、Harness 和 API 配额也属于不同作用域。

HTTP 同步助手入口通过 AnyIO worker 的父循环执行 Agent，避免每次请求重建独立并发门。短生命周期 UI 循环在退出前关闭自己的异步客户端。

legacy Workflow 可实时生成流；catalog 与 Agent 先检查再分块。SSE 的 token 事件不保证每个事件恰好一个模型 Token。取消关闭本次响应，未完成生成不写完整会话；服务商是否停止计算或计费需另验。

详见 [异步与限流](ASYNC_ARCHITECTURE.md) 和 [SSE 取消](SSE_DISCONNECT_HANDLING.md)。

## 9. 服务与部署边界

FastAPI 支持 JSON、SSE、双语 OpenAPI 和只读实体查询，不暴露 Publisher。可选 Bearer Token 验证不等于完整身份系统；thread_id 与 kb_id 也不等于租户身份。

当前使用单 Worker，默认仅监听 loopback。公网需要另外设计 TLS、身份、权限、限流、密钥管理与审计保留策略。

Docker 配置使用非 root、只读根文件系统、capability 移除、no-new-privileges 和命名卷，真实发布关闭。代码配置与当前镜像验收不同：2.8 镜像有历史证据，本轮功能未重新构建验收，不提供生产就绪保证。

## 10. 测试与接手顺序

pytest 验证数据契约、门控、权限、超时、取消、事务和失败分支。模拟模型使控制流可重复且不消费外部配额；它不能证明真实模型的行动或回答质量。

真实模型冒烟独立运行；固定教学题不作为盲测。离线回归集与调参数据不能再充当最终独立测试集。

接手顺序：

1. 从 README 启动离线工作台与 API。
2. 阅读 types、assistant、retrieval_agent 和 tool_gateway，核对实际权限与循环。
3. 阅读 rag、knowledge、knowledge_store，运行入库/更新/删除测试。
4. 阅读 approval、publish_safety 和 Publisher，先用 DryRun。
5. 跑完整门禁，再按需要做本机模型或容器验证。

当前结果、保留的失败实验和后续验收分别见 [验证记录](TEST_RESULTS.md)、[更新日志](CHANGELOG.md) 与 [计划](../PLAN.md)。
