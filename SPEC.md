# SPEC.md — PersonaX 架构硬约束

## 1. 三层解耦（对齐商用框架）
| 层 | 模块 | 职责 | 不允许 |
|---|---|---|---|
| 生成层 | skills/ + llm.py | 内容生成、工具调用 | 编排、发布 |
| 编排层 | orchestrator.py + graph.py | 工作流、重试、状态 | 直接调 LLM |
| 执行/管控层 | publishers/ + harness.py | 副作用、规则拦截 | 业务逻辑 |

## 2. 数据契约（types.py）
- `Draft`：Skill 间传递的唯一结构化对象（禁止裸 dict）。
- `SkillInput` / `SkillOutput`：Skill 统一签名。
- `ExecutionContext`：编排→Skill 上下文快照（含 checkpoint）。

## 3. Skill 标准（manifest）
每个 Skill 必须声明：name / description / triggers / popularity / version。
路由 = 语义(0.5) + 关键词(0.3) + 热度(0.2)，见 `registry.route()`。

## 4. 风格约束（StyleEnforcer）
- 硬约束（forbidden / 句长）→ `ok=False`，编排层触发重写（最多 max_retries）。
- 软约束（emoji 密度、互动引导）→ 仅记录，不阻断。

## 5. 管控（Harness）
- allow/deny、rate_limit、sensitive_tool approval、audit log。
- 规则引擎**独立于业务**，可替换（未来接 RBAC / 脱敏）。

## 6. RAG
- 四索引：raw / summary / hypo（假设性问题）/ metadata。
- 四查询：enrich / multi_query / decompose / rerank(RRF)。
- 生产环境把 `VectorStore` 替换为 Milvus/Qdrant，接口不变。

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
