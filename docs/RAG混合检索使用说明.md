# PersonaX 混合检索使用说明

更新日期：2026-10-15。发布基线为 2.8.1；新文档目录库属于未发布工作区增量。

## 先区分两个知识入口

| 入口 | 数据与生命周期 | 调用方式 | 输出 |
|---|---|---|---|
| 原内容 / Web / API 助手 | `knowledge/*.md`，原目录加载和索引缓存 | `build_rag_from_dir`，现有助手与内容生成 | 原助手可以基于来源生成回答 |
| 新本地文档目录库 | MD/TXT/HTML/文本 PDF 导入 SQLite 当前快照，有版本、任务、软删除 | CLI answer、FastAPI、工作台显式选择 catalog | 默认摘录；显式生成、引用检查、失败回退和无证据拒答 |

两条路径复用检索实现，不隐式复制数据。工作台可选择数据源，知识库页可只读浏览目录库和查询实体关联；导入/删除仍走本地 CLI。操作、Qdrant 本地适配和证据边界见 [目录库](KNOWLEDGE_BASE.md)，图示见 [项目流程图](知识图谱.md)。

## 实际链路

下图是原目录 RAG 的可配置检索链；LLM 改写、HyDE、Cross-Encoder 均是可选组件，不表示新 CLI 已启用这些增强。

`规则/LLM 查询增强 → BM25 + dense embedding 精确 kNN → RRF → 可选 cross-encoder → min_score 门控`

- BM25、精确 kNN 和 RRF 均由 `core/rag.py` 实现，不需要额外向量数据库。
- 默认 `hashing` 用于离线特征检索，不是语义向量模型。
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

原目录 RAG 有 120 条工程回归样例，历史基线与消融集中在 [RAG 记录](RAG_EVAL_2_3.md)。它不是独立真实用户质量集，不同数据集的结果不能直接相减为提升。

新目录库另有 `eval/knowledge_stage1_cases.json` 的 6 条自编工程冒烟，4 条来源定位、2 条无关查询，明确标记 `independent_domain_evaluation=false`。不能混称为原 RAG 评测，也不能据此宣称领域准确率或海量效果。

## 新目录库的最小操作闭环

在项目根目录执行；以下默认离线，不调用生成模型或 Ollama：

```powershell
python main.py knowledge ingest --file rag.md --license project-authored-demo
python main.py knowledge ingest --file rag.md --license project-authored-demo
python main.py knowledge list
python main.py knowledge search --query "BM25 与 RRF 如何配合"
python main.py knowledge jobs
python -m eval.knowledge_scorer
```

第二次相同导入返回 `unchanged`。默认源目录为 `examples/ai_engineering`，数据库为 `logs/knowledge.sqlite3`；自定义 `--db / --source-root / --kb-id / --chunk-size` 要放在 `ingest / search` 等动作前。

显式选择 `search --backend ollama --model bge-m3 --min-score 0.50` 才调用真实向量模型；阈值须按领域重新校准。目录库新建的 `RAGPipeline` 不自动读取旧目录路径的 LLM 改写/HyDE/重排配置。PDF 需安装可选依赖 `pip install -e ".[ingest]"`，且不支持 OCR。

更新/删除提交后，新查询重新取 active 快照；代价是每次查询的加载与建索引开销。已增加可选 Qdrant 本地持久化后端，但仍按快照 upsert，并不代表服务端 ANN 或最优增量索引。评估时要同时测建索引、查询、资源和人工相关性，不只比较向量检索耗时。

删除、恢复、格式限制及失败处理见 [目录库操作](KNOWLEDGE_BASE.md)。当前新增功能没有重建 Docker，宿主机结果不构成容器验收。
