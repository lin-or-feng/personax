# PersonaX 混合检索使用说明

## 实际链路

`规则/LLM 查询增强 → BM25 + dense embedding 精确 kNN → RRF → 可选 cross-encoder → min_score 门控`

- BM25、精确 kNN 和 RRF 均由 `core/rag.py` 实现，不需要额外向量数据库。
- 默认 `hashing` 只保证离线测试稳定，不应在简历中称为“语义向量模型”。
- 正式语义检索推荐使用本地 Ollama `bge-m3`。
- 当前对所有 chunk 计算余弦相似度并取 Top-K，逻辑直观、结果精确，适合项目现阶段的小知识库。
- 等知识库增长到数万甚至更多 chunk，且实测延迟成为瓶颈后，再替换为 HNSW/FAISS 等 ANN 索引。
- Query rewriting 默认采用可解释规则。只有复杂长查询或确有评测提升时再启用 LLM。
- Cross-encoder 只重排少量候选，不能用于全库召回。

## 启用真实语义向量

```powershell
ollama pull bge-m3
```

在 `.env` 中加入：

```env
RAG_EMBEDDING_BACKEND=ollama
RAG_EMBEDDING_MODEL=bge-m3
RAG_QUERY_ENHANCER=rule
RAG_ENABLE_HYDE=0
RAG_RERANKER=none
```

## 启用 LLM query rewriting / HyDE

```env
RAG_QUERY_ENHANCER=llm
RAG_ENABLE_HYDE=1
```

这会通过 `core.llm.complete()` 额外调用本地 Qwen 或云端模型。短主题通常不值得增加延迟，必须用评测结果决定是否开启。

## 启用 cross-encoder

```powershell
python -m pip install -e ".[rag]"
```

```env
RAG_RERANKER=cross-encoder
RAG_RERANKER_MODEL=BAAI/bge-reranker-base
```

首次运行会下载模型。建议只对 RRF 召回的前 10～30 个候选重排。

## 知识数据格式

知识不是从向量数据库“获取”的；先准备有权使用的 Markdown 文本，再由 embedding 模型生成向量。每个文件建议使用：

```markdown
---
topic: 秋招面试
style: 清单式干货
keywords: [简历, 面试, 武汉, 校招]
---
正文内容……
```

优先使用自己的文章、官方公开资料、已授权数据和自己整理的摘要。先做 200～400 篇、约 1000～3000 个 chunk，再考虑真正的大规模向量数据库。

## 评测

```powershell
python -m eval.rag_scorer --top-k 1
```

输出 `Recall@K`、`MRR`、embedding 后端、kNN 检索模式、是否启用重排。修改检索策略前后必须用同一评测集对比。

仓库自带的 6 条评测数据只用于回归冒烟，不能当作简历性能结论。要得到可信结果，应扩充到至少 50～100 条人工标注查询，并分别对比 BM25、dense kNN、RRF、RRF + reranker。
