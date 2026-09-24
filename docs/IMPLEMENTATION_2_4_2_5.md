# PersonaX 2.4 / 2.5 实施说明

> 实施日期：2026-09-23
> 范围：本地单实例 Agent；不执行真实发布，不把 Redis、ANN 或远程模型强行加入默认路径。

## 1. 版本拆分

### 2.4：可靠性与安全闭环

2.4 解决的是“Agent 能运行，但失败边界和人工审批不够硬”的问题。

1. **持久化 HITL**：`core/approval.py` 使用 LangGraph `interrupt` 暂停发布审批，使用 `Command(resume)` 恢复；`SqliteSaver` 与审批索引保存在 `logs/langgraph_checkpoints.sqlite3`。
2. **审批幂等**：草稿标题、正文、标签、主题与封面计算稳定指纹；同一用户和草稿只允许一个 pending / approved / consumed 审批。成功发布后审批转为 `consumed`，重复调用只返回已发布结果。
3. **四重运行预算**：`AgentRunBudget` 同时约束步骤数、估算 Token、总时限与重复状态次数。预算检查在 Supervisor、Retriever、Answerer、Reviewer 的真实执行点发生。
4. **工具错误语义**：工具结果区分 `business_empty`、`validation`、`permission`、`timeout`、`unavailable`、`budget` 与 `internal`；仅 timeout / unavailable 执行有界指数退避。
5. **统一关联标识**：每个 `AssistantRequest` 自动生成 `trace_id`，贯穿工具网关、审计和回答结果。
6. **确定性红队**：`eval/security_eval_set.json` 覆盖知识/记忆提示词边界、危险 URL、未知工具与 PII；默认完全离线。

发布门禁顺序：

```text
草稿检验通过
  → 提交审批（LangGraph interrupt + SQLite checkpoint）
  → 人工批准（Command(resume)）
  → 重新校验用户 + 草稿指纹 + approval 状态
  → 真实发布安全锁 + Harness 配额 + 登录态
  → 平台操作 + AI 声明回读
  → 成功后 approval=consumed
```

人工审批不是新的绕过路径。后端 Publisher 会再次读取 SQLite 并校验草稿指纹，不能只依赖 Streamlit 按钮状态。

## 2. 2.5：RAG 与记忆治理

### 2.1 标题感知 Parent-Child Chunk

构建索引时先按 Markdown 标题拆父段，再对父段执行固定长度子块切分：

```text
Markdown 文档
  → 标题父段（完整语义上下文）
  → 子块（BM25 / dense 精确 kNN / RRF / 可选重排）
  → 命中子块
  → 向 Answerer 展开父段 context_text
```

这样保留“小块检索更精确、完整段落回答更连贯”的两种优势。Cross-Encoder 仍对候选子块评分，避免父段过长影响重排成本。

每个 chunk 记录：

- `document_version`：原始文件内容 SHA-256 短哈希；
- `index_version`：本轮全部知识文件签名的 SHA-256 短哈希；
- `parent_id` / `parent_heading` / `chunk_index`；
- `chunk_strategy=heading_parent_child` 写入 RAG trace。

新索引只在全部文档完成读取和向量构建后进入进程缓存。embedding 构建失败时返回可降级的 BM25 实例，但不缓存失败实例；服务恢复后下一次调用会重新构建。

### 2.2 Cross-Encoder 消融

默认 CI 不下载 Cross-Encoder。已准备公平对照入口：

```powershell
.\.venv\Scripts\python.exe main.py rag-eval `
  --compare-reranker `
  --backend hashing `
  --mode hybrid `
  --report-md docs/RERANKER_ABLATION.md
```

对照固定评测集、召回方式、Top-K 与门控阈值，只改变重排器。报告同时给出门控 Recall@K、MRR、负例拒检、P95、降级状态。未安装模型时明确标记 `reranker` 降级，不把 RRF 回退结果冒充 Cross-Encoder 成绩。

### 2.3 长期记忆治理

- **写入最小化**：只接受用户主动保存；模型生成的摘要只能成为候选。
- **摘要确认**：取最近 8 条消息生成不超过 120 字候选，用户可编辑，确认后才写 SQLite。
- **PII 掩码**：保存前处理常见中国手机号、邮箱和身份证号，并记录 `redacted`。
- **生命周期**：支持不过期或 30 / 90 / 365 天 TTL；查询时自动排除过期数据。
- **可迁移与删除**：UI 可导出 JSON，也可二次确认后删除当前用户全部长期记忆。
- **来源可追踪**：保存 `kind` 与 `source_thread_id`，区分偏好和摘要。

## 3. 前后对比与本轮证据

| 项目 | 2.3.x | 2.4 / 2.5 |
|---|---|---|
| 发布确认 | 当前 Streamlit 会话内二次点击 | 可跨重启 interrupt/resume，后端重验，成功后消费 |
| Agent 上界 | 最大步骤 | 步骤 + Token + 总超时 + 循环检测 |
| 工具失败 | 统一 error | 分类、有限重试、次数与 trace_id |
| RAG 上下文 | 命中固定子块 | 子块召回，父段注入 |
| 索引追踪 | 文件签名只用于缓存 | 文档版本、索引版本与切块策略进入元数据/trace |
| 长期记忆 | 显式 CRUD | PII、TTL、摘要确认、导出、删除全部 |
| 安全评测 | 分散单测 | 16 条数据驱动红队 + CI 发布门禁 |

2026-09-23 本地 hashing 回归：父子 Chunk 与旧版固定切块的 Recall@5、门控 Recall@5、MRR、负例拒检完全一致，分别为 **0.8889 / 0.8056 / 0.7775 / 0.75**。这说明本次上下文扩展没有牺牲检索排序；回答语义质量仍需后续增加人工标注或 LLM-as-Judge 评测，不能只用召回指标代替。

## 4. 为什么暂不引入 ANN 和 Redis

- 当前知识库规模没有证据显示精确 kNN 是 P95 瓶颈；ANN 会引入索引参数、近似召回损失与额外依赖。达到约一万以上 chunk 并测出向量遍历瓶颈后，再用 FAISS/HNSW 做同集消融。
- 当前运行形态是单实例本地 Streamlit；SQLite checkpoint、审批和记忆能提供更小的部署面。只有多进程/多机器共享会话、限流或锁成为真实需求时，才迁移 Redis。
- Cross-Encoder 保持可选，避免为默认 Docker/CI 下载数百 MB 模型；是否启用由消融收益决定。

## 5. 验收命令

```powershell
# 134 项单元测试
.\.venv\Scripts\python.exe -m pytest

# 16 条离线红队
.\.venv\Scripts\python.exe -m eval.security_scorer --summary-only

# 120 条父子 Chunk RAG 门禁
.\.venv\Scripts\python.exe -m eval.rag_scorer --backend hashing --mode hybrid --top-k 5 --summary-only

# 旧固定切块对照
.\.venv\Scripts\python.exe -m eval.rag_scorer --backend hashing --mode hybrid --top-k 5 --flat-chunks --summary-only

# 全发布门禁
.\.venv\Scripts\python.exe scripts/release_check.py
```

真实发布不属于自动测试范围；所有回归均不登录、不调用小红书、不产生平台副作用。
