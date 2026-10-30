# SPEC.md — PersonaX 架构硬约束

## 1. 分层边界
| 层 | 模块 | 职责 | 不允许 |
|---|---|---|---|
| 生成层 | skills/ + llm.py | 内容生成、工具调用 | 编排、发布 |
| 编排层 | orchestrator.py + graph.py + assistant.py | 工作流、重试、状态 | 自行构造模型客户端或绕过统一入口 |
| 执行/管控层 | publishers/ + harness.py | 副作用、规则拦截 | 业务逻辑 |

## 2. 数据契约（types.py）
- `Draft`：Skill 间传递的唯一结构化对象（禁止裸 dict）。
- `SkillInput` / `SkillOutput`：Skill 统一签名。
- `ExecutionContext`：编排→Skill 上下文快照（含 checkpoint）。

## 3. Skill 标准（manifest）
每个 Skill 必须声明：name / description / triggers / popularity / version。
路由按触发词 token 重叠(0.5)、关键词匹配(0.3)和热度(0.2)加权，见 `registry.route()`；不是模型语义规划。

## 4. 风格约束（StyleEnforcer）
- 硬约束（forbidden / 句长）→ `ok=False`，编排层触发重写（最多 max_retries）。
- 软约束（emoji 密度、互动引导）→ 仅记录，不阻断。

## 5. 管控（Harness）
- allow/deny、rate_limit、sensitive_tool approval、audit log。
- 规则引擎**独立于业务**，可替换（未来接 RBAC / 脱敏）。

## 6. RAG
- BM25 与 dense 精确 kNN 双路召回，RRF 融合；查询增强和 Cross-Encoder 由配置选择。
- Chunk 保留原文与来源元数据；查询只使用授权目录的有效文档快照。
- 存储适配必须保持检索接口和快照过滤。Qdrant 当前为可选本地模式，不等同于生产服务验收。

## 7. 发布
- `Publisher` 抽象，`DryRunPublisher`（测试）+ `XhsPlaywrightPublisher`（真实）。
- 真实发布**必须人工确认 + 限速 + cookie 隔离 + 幂等 + 失败重试**。
- `PublishScheduler`（content_bank 定时调度）逐稿：到期检查 → 内容补齐 → 合规 → 发布 → `publish_log.json` 留痕。

## 8. 合规
- `core/compliance.py`：广告法绝对化用语 / 医疗金融承诺 / 平台导流词表，发布前必检，命中即拦截。
- 词表独立于业务（`config/compliance.yaml`），可热更新，fail-safe 内置兜底。

## 9. 评估
- `eval/eval_set.json`：每条含 expected_traits + forbidden。
- `eval/scorer.py`：trait / emoji / 句长三维打分 → CSV。
- CI 门禁：平均分不得低于上一版本（回归保护）。

## 10. 受控检索 Agent（工作区增量，非发布版本）
- `AssistantRequest.execution_mode=workflow|agent`，默认 workflow；发布流程不变。
- `RetrievalAgentDecision` 是模型唯一决策契约：search / finish / clarify / insufficient_evidence，拒绝额外字段；模型只能提供检索词，不能指定工具名、路径、用户身份、库或阈值。
- `core/retrieval_agent.py` 根据工具观察循环决策；只读检索必须经过现有 StructuredTool / Gateway / Harness。执行参数及授权由服务端注入。
- `max_steps` 在 Agent 模式约束模型决策、工具执行与回答生成；默认 UI 为 8，检索最多 3 次。重复查询、预算/时间用尽必须停止；无新增证据后禁止继续检索，仅允许收尾决策；引用校验不因步数耗尽而跳过。
- 模型调用使用可取消的异步入口，单次和整轮期限受限；同步检索在线程中等待安全收尾，不能声称能强制终止 Python 线程。
- 无证据不生成事实结论；规划器失败只允许有预算时以原问题执行一次确定性检索并摘录，不把降级称为成功规划。
- Agent 输出先完成证据检查，再分块展示；不是未经检查的 token 实时流。会话 checkpoint 保存完成的对话，不宣称支持循环中途恢复。
- 审计只存动作、计数、哈希、耗时和停止码，不新增问题/证据正文持久化；跨层使用 types.py 契约。

## 11. 公开文件检查
- 简历、个人文档、渲染图片、凭据和运行状态不属于代码交付，须在 Git 和 Docker 构建上下文中排除。
- 隐私检查只处理 Git 当前可纳入版本的文件，输出文件位置和问题类型，不回显命中的个人信息；第三方文档的合法署名不删除。
- 历史文件内容可单独审查；检查不修改提交历史，不等于检查远端仓库、提交作者信息或图片中的文字。
- 文档按用途保留一个入口；当前能力、历史测量和未验收内容分开，避免重复内容与开发对话残留。

## 12. P0 本地扫描件导入契约
- `DocumentParseOptions` 是操作者配置，不是模型工具参数；默认关闭 OCR。`DocumentParseResult` 包含正文单元和逐页诊断，`OCRPageResult` / `OCRBlock` 是进程边界的唯一识别协议。
- PDF 逐页分流：可信原生文本直接提取；无文本、异常文本或含栅格图像的页在显式开启后进入本地 OCR；可证明无内容的空白页记录但不入索引。不允许静默丢弃未识别页。
- OCR 运行在有超时的独立子进程，CPU 推理，读取已准备且校验哈希的本地模型。导入时不下载模型、不调用云服务、不执行 PDF 中的脚本。子进程隔离不是恶意文件安全沙箱。
- 每个 OCR 正文块携带页码、归一化包围框、引擎版本、模型指纹、置信度与质量标记；置信度不是准确率。先支持印刷体单栏，不承诺表格、公式、手写或复杂版面。
- 所有 OCR 内容标记 `ocr_unverified`，低置信度另标 `low_confidence`；本地 `knowledge inspect` 可明确预览正文，`ingest --accept-ocr-review` 表示操作者已核对，未确认时不提交目录版本。不得自动用 LLM 改写、补全金额/否定词。
- 字节、页数、文本、OCR 块数、渲染像素和总时间均有边界。任何页失败、超限、未确认或模型校验失败，整份导入失败并保留旧版本；任务日志只记录固定原因码，不记录正文、模型路径或异常栈。
- 原文件哈希、解析/分块策略、OCR 配置与模型指纹共同决定版本；识别位置与质量信息传递到检索来源。OCR 产物与模型放在忽略目录，不进入 Git 或 Docker 构建上下文。
- P0 不开放 HTTP 上传、不新增用户系统/任务队列、不更改发布权限；全栈上传与任务界面属于下一阶段。

## 13. P1 本机文档工作台契约
- 独立 React/TypeScript 前端调用 FastAPI `/v1/workspace/*`，本地启动器显式开启；默认服务保留原行为。新接口强制 Bearer Token，拒绝跨 Origin 和非本机 Host，不把密钥放入 URL、浏览器持久化或日志。
- 这是单操作者、单 API 进程/单导入 Worker 的本机工作台，不宣称多租户认证、分布式队列或公网生产就绪。助手仍无上传、文件读取和发布工具。
- 上传只收有界二进制正文与编码文件名，禁止路径和 URL；服务端生成任务 ID 与私有存储路径。单文件、总任务/存储预算、状态变更和模型路径均由服务端约束。
- `WorkspaceJob`、`WorkspacePreview`、`WorkspaceReviewRequest`、`KnowledgeReviewedParse` 是任务/审核契约。状态为 queued → parsing → awaiting_review → queued_commit → committing → completed，失败/取消显式记录。Worker 用 SQLite 领取与租约标识防止旧解析覆盖新预览；过期任务进入明确失败，可由操作者重试。
- 原件不可变；预览持久化但不是审计正文。预览哈希绑定原件哈希、解析策略、目录预期 revision 及修订正文。确认请求必须带当前任务 revision 与预览哈希，重复确认幂等，过期确认返回 409。
- 提交使用已经预览的结构化正文，禁止确认后再次 OCR。提交前重新核对原件、模型/解析策略和目录 revision，保留目录事务/并发保护；任务库与目录库不是分布式事务，以确定版本重试实现提交结果对账。
- 人工编辑不抹掉原 OCR 质量标记，并标注 human_edited。审核后的 Chunk/引用必须可定位到所属任务的原页；原件及页图均需鉴权，不返回操作者文件系统路径。
- 取消排队/待审核任务不会入库；提交进行中不伪装可撤销。预览、页图、JSON 回答均 no-store；浏览器采用文本渲染，无外部脚本/字体/分析请求。
- P1 持久化导入任务和私有预览，不默认持久化浏览器提问；本轮只验证受信资料、本机 UI/HTTP 闭环。OS 级恶意文档隔离、独立质量集、用户系统、存储自动保留策略与新容器复现仍待后续验收。
