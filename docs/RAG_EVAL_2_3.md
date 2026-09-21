# PersonaX 2.3 RAG 评测记录

> 日期：2026-09-17（Asia/Shanghai）
> 边界：本地知识库与本机 Ollama；未联网搜索、未登录小红书、未执行真实发布。

## 数据与方法

- 知识库：27 份 Markdown，构建后 156 个 chunk。
- 回归集：120 条；108 条有答案查询、12 条知识库外查询。
- 难度：exact、standard、paraphrase、hard、negative。
- 分类：Agent/RAG、校园、学习、求职、武汉生活、就业保障和知识库外问题。
- 指标：Recall@5、门控 Recall@5、MRR、无答案拒检准确率、P50/P95/P99、组件降级。
- 门控 Recall 会先过滤低相关候选再取前 5 个来源，因此可能高于未门控的 RRF Top-5 Recall。

这些样例用于工程回归，不代表真实用户问题分布，也不是线上 SLA。公开百科之间存在主题重叠，后续还需要人工相关性复核和真实用户问题集。

## 可复现离线基线

运行：

```powershell
& .\.venv\Scripts\python.exe main.py rag-eval --compare --backend hashing `
  --summary-only --report-md docs\RAG_ABLATION_2_3.md
```

| 模式 | Recall@5 | 门控 Recall@5 | MRR | 无答案拒检 | P95 |
|---|---:|---:|---:|---:|---:|
| BM25 | 0.9074 | 0.2037 | 0.7568 | 1.0000 | 142.3 ms |
| hashing dense kNN | 0.8519 | 0.8333 | 0.7375 | 0.5833 | 29.7 ms |
| hybrid + RRF | 0.8889 | 0.8056 | 0.7775 | 0.7500 | 168.1 ms |

在统一门槛 `0.15` 下，hybrid 是唯一同时通过 Recall、门控 Recall、MRR 和负例拒检四项门禁的模式。BM25 原始召回高，但当前归一化相关性门控不适合直接使用 BM25 原始分数；hashing dense 只是离线特征兜底，不应被解释为真实语义 embedding。

## 本机 bge-m3 语义复测

运行：

```powershell
& .\.venv\Scripts\python.exe main.py rag-eval --backend ollama `
  --top-k 5 --min-score 0.50 --summary-only
```

| 指标 | 结果 |
|---|---:|
| Recall@5 | 0.9537 |
| 门控 Recall@5 | 0.9815 |
| MRR | 0.8035 |
| 无答案拒检准确率 | 1.0000 |
| P50 | 152.8 ms |
| P95 | 388.5 ms |
| P99 | 433.2 ms |
| 文档向量缓存 | 156 hits / 0 misses |
| 降级组件 | 无 |

阈值扫描显示，`bge-m3` 在本数据集上使用 `0.10` 到 `0.30` 时，12 条负例全部会误召回；提高到 `0.50` 后负例拒检达到 1.0，同时门控 Recall@5 为 0.9815。因此 2.3 将 Ollama 默认门槛校准为 `0.50`，hashing 保持 `0.15`。不同模型的 cosine 分布并不相同，替换 embedding 模型后必须重新扫描阈值。

## 已落地的质量保护

- 评测聚合全部 120 条查询的降级组件，不再只读取最后一条 Trace。
- 同一来源的多个 chunk 不重复占满助手来源列表。
- Ollama 文档建索引失败时保留 BM25，并在 Trace 标记 `dense_index` 降级。
- 回答中的引用编号必须落在实际来源范围内。
- 走知识检索但没有来源时，Reviewer 强制声明“本地知识库未检索到可核验资料”。

## 下一步

1. 对 paraphrase、hard 和 job 分类的失败样例做人工相关性复核。
2. 安装并评估 Cross-Encoder，比较质量增益、显存和延迟。
3. 收集脱敏后的真实用户问题，另建不参与开发调参的 holdout 集。
4. 增加答案级事实支持度评测；在人工集准备好前，不宣称已解决幻觉问题。
