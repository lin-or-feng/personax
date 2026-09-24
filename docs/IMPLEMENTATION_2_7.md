# PersonaX 2.7：真实反馈与排序实验闭环

## 1. 为什么没有继续堆基础设施

PersonaX 当前是本地单实例内容 Agent，知识库仍适合精确 kNN，SQLite 也足够承载会话、审批和反馈。此时增加 ANN、Redis、多副本或新的模型服务，会提高安装与解释成本，却没有数据证明它们解决了真实瓶颈。

2.7 因此只做两件事：收集真实失败回答，并让 Cross-Encoder 对照拥有更合适的排序指标。

## 2. 反馈数据契约

`AssistantFeedbackStore` 使用 SQLite，一条 `trace_id` 只保留一条最新反馈：

- 默认字段：rating、reason、Trace、thread、route、source_count、degraded、answer_hash 和时间。
- 默认不保存：完整问题、完整回答、知识片段、提示词、工具参数或密钥。
- 显式同意后：保存最多 500 字的问题与回答摘要，以及来源编号、标题和文件定位；不复制来源正文。
- 常见手机号、邮箱、身份证号及 `sk-`/Bearer 形式的凭据在写入前掩码，并记录 `redacted=true`。

回答哈希只用于识别回答是否变化，不能还原正文。反馈库位于：

```text
logs/assistant_feedback.sqlite3
```

`logs/` 已被 Git 忽略；Docker 部署时由现有日志命名卷持久化。
状态页提供二次确认的“删除全部反馈”，用户可以随时清除本机反馈库内容。

## 3. 从负反馈到评测样例

点踩会立即记录最小化反馈。用户可以继续选择原因：事实不准确、缺少或引用错误、答非所问、回答不完整、响应太慢或其他。

只有勾选“同意在本机保存脱敏后的问题与回答摘要”并提交后，该记录才会出现在候选导出中：

```powershell
& .\.venv\Scripts\python.exe main.py feedback
& .\.venv\Scripts\python.exe main.py feedback --output artifacts\feedback_candidates.json
```

候选固定标记为 `needs_human_label`。进入正式评测集前必须人工完成：

1. 判断问题是否属于知识库范围。
2. 标注正确来源或确认无答案。
3. 校正失败原因，移除残余隐私信息。
4. 去重后分入开发集或固定回归集，不能同时用于调参和最终报告。

## 4. NDCG@K

Recall@K 只回答“正确来源是否出现”，MRR 更关注第一个正确来源。一个问题可能对应多个正确来源，因此 2.7 增加二元相关性 NDCG@K：

- 越多正确来源排在前面，得分越高。
- 同时计算门控前和 `min_score` 门控后的 NDCG。
- Cross-Encoder 消融的赢家选择依次考虑门控 Recall、门控 NDCG、MRR、无答案拒检和 P95。

可复现的离线 hashing 基线：

```powershell
& .\.venv\Scripts\python.exe -m eval.rag_scorer `
  --top-k 5 --backend hashing --mode hybrid --min-score 0.15 --summary-only
```

本轮测得 NDCG@5=0.8049、门控 NDCG@5=0.7710；原有 Recall、MRR 和拒检指标保持不变。

## 5. Cross-Encoder 边界

项目已经提供 `--compare-reranker`，但默认环境不安装重排模型。本轮只完善指标和报告，不自动下载模型，也不记录虚假的 Cross-Encoder 提升。

在磁盘空间允许并安装可选依赖后，使用同一数据集运行：

```powershell
& .\.venv\Scripts\python.exe main.py rag-eval `
  --compare-reranker --backend ollama --min-score 0.50 `
  --report-md artifacts\reranker_ablation.md
```

只有报告显示 Cross-Encoder 在可接受 P95/显存成本下稳定提高门控 NDCG 或 MRR，才应默认开启。
