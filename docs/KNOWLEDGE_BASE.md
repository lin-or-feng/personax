# 文档目录库

更新：2026-10-22。目录库提供版本化的本地文档管理；原 `knowledge/*.md` 内容范例目录保留，两者不隐式同步。

## 数据与资源限制

- 支持 UTF-8 MD/TXT/HTML 和文本型 PDF。PDF 需 `pip install -e ".[ingest]"`，不支持 OCR。
- 默认单文件 5 MiB、提取文本 500,000 字符、PDF 100 页、每文档 2,000 Chunk。
- 导入路径相对操作者配置的 `source_root`，拒绝绝对路径、`..`、软硬链接与越界读取。
- HTML 不执行脚本或抓取链接，去除 head、导航和侧栏；解析器不是进程级恶意文件沙箱，只应处理受信资料。

## 导入与查询

在仓库根目录执行；默认自编资料位于 `examples/ai_engineering`，数据库为 `logs/knowledge.sqlite3`。

```sh
python main.py knowledge ingest --file rag.md --license project-authored-demo
python main.py knowledge ingest --file context.html --license project-authored-demo
python main.py knowledge ingest --file operations.txt --license project-authored-demo
python main.py knowledge list
python main.py knowledge search --query "BM25 与 RRF 如何配合"
python main.py knowledge jobs
```

全局参数必须在动作前：

```sh
python main.py knowledge --source-root ./my-documents --kb-id engineering ingest --file guide.pdf
python main.py knowledge --kb-id engineering search --query "检查点恢复"
```

相同文件重复导入返回 `unchanged`；修改后重新导入生成新版本。不自动监视目录，也不会因磁盘文件消失而删除记录。

## 公开资料

```sh
python scripts/import_python_catalog.py
python scripts/build_python_graph.py
python main.py knowledge --kb-id python-official answer --query "Semaphore acquire release 信号量"
python main.py knowledge --kb-id python-official graph --entity Task --hops 2
```

导入脚本首次主动下载 Python 官方资料，之后校验缓存并幂等导入。当前 10 份文档、531 个 Chunk；保留来源 URL、许可页、SHA-256 和获取信息。Chunk 数量变化不是质量提升指标。

默认 hashing 只用于离线工程测试。真实语义向量可显式选择：

```sh
python main.py knowledge --kb-id python-official answer --query "Semaphore acquire release" --backend ollama --model bge-m3:latest --min-score 0.5
```

相关性阈值需按模型与语料校准。此处 `--model` 指 embedding 模型，不是回答模型。

## 助手、工作台与 API

在助手选择 `knowledge_source=catalog` 和已授权 `kb_id`。任务支持 `qa / compare / summary`，compare 至少需要两个独立文档。

Workflow + `answer_mode=extractive` 不调用回答模型；显式 `llm` 时先生成、检查后输出，失败回退摘录。Agent 模式另需行动决策模型，协议见 [Agent](RETRIEVAL_AGENT.md)。无证据不生成事实结论。

服务端在 `config/persona.yaml` 的 `knowledge_catalog` 配置数据库、根目录、允许库清单和后端。`PERSONAX_KNOWLEDGE_DB` 仅供操作者覆盖路径。`kb_id` 只是分区，不是租户认证。

工作台/API 只读浏览、问答和实体查询；导入、删除与恢复使用本地 CLI，未开放任意 URL 下载或文件读取工具。

## 更新、删除和中断

文档身份保持稳定，版本由字节内容、来源与解析/分块配置共同决定。文档、Chunk 和任务完成事务提交；并发写入以预期版本和单调 revision 防止旧任务覆盖新数据。

解析失败保留旧可用版本。删除提交后新查询排除该文档，但不撤销已经开始的快照。

```sh
python main.py knowledge --kb-id engineering delete --document-id DOCUMENT_ID --yes
```

删除是目录软删除，不删除原文件或安全擦除历史字节；再次导入可恢复可查询状态。

确认没有导入进程运行后，才能处理遗留 running 任务：

```sh
python main.py knowledge jobs
python main.py knowledge recover --yes
```

恢复将遗留任务标为 abandoned，之后需重新导入；没有自动续跑或分布式任务队列。

## Qdrant 与实体关联

```sh
python -m pip install -e ".[vector]"
python main.py knowledge --kb-id python-official answer --query "Semaphore" --vector-store qdrant
```

Qdrant 当前是本地持久化模式。查询同时限制库标识和有效快照 point ID，旧向量即使尚未物理回收也不能进入新证据。它不是 Qdrant Server、ANN 集群或高可用部署验收。

实体图是 20 条原文 `co_mentions` 关系，查询最多两跳，排除过期/删除文档的边。它表达共现，不表达因果，也不等于完整 GraphRAG。

## 验证与限制

`tests/test_knowledge_base.py` 覆盖事务、冲突、更新/删除、路径、格式和恢复；catalog、Qdrant 与实体查询分别有专项测试。

`eval/knowledge_stage1_cases.json` 的 6 条用例与 Python 目录库的 18 条来源诊断均为开发固定集；百份合成文档测试仅验证目录稳定性。真实模型、容器与独立质量边界见 [验证记录](TEST_RESULTS.md)。
