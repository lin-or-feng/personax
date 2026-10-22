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
