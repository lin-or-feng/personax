# PersonaX

本地内容生成与知识问答工具，采用确定性发布 Workflow 与受控检索 Agent。支持 CLI、Streamlit 和 FastAPI；真实发布默认关闭。

最新源码包含版本化目录库、受控检索 Agent、可选 OCR 和实验性本地文档工作台；历史发布标签基线为 2.8.1，新增能力尚未作为独立版本发布。

## 功能

- 内容工作流：多人格提示词、标题/正文/标签生成、风格检查、封面与合规检测。
- 检索：BM25 + 稠密向量精确 kNN，RRF 融合；按配置启用查询增强和 Cross-Encoder。
- 文档目录库：MD、TXT、HTML、文本型 PDF；可选本地 CPU OCR 支持扫描/混合 PDF 和 PNG/JPEG，预览与人工确认后入库，保留页码、文字框与质量标记。见 [OCR 用法与边界](docs/DOCUMENT_OCR.md)。SQLite 版本管理、软删除、来源定位，可选 Qdrant 本地持久化。
- 助手：固定 Workflow 或模型驱动的只读检索循环，支持补查、澄清、引用检查、会话恢复和显式长期记忆。
- 工具管控：StructuredTool、Pydantic 参数校验、Harness 限流与审计。助手没有发布工具。
- 发布：Playwright 操作、人工确认、安全锁、AI 声明检查及结果核验。容器内禁用真实发布。

## 文档工作台（实验功能）

新增基于 React / FastAPI 的单机文档工作台，支持上传、解析任务、预览及显式确认后入库。该功能默认关闭，仅面向受控的本地单操作者，不应直接暴露到公网。工作台与 OCR 的完整浏览器端联调尚未作为正式验收完成；请先使用下面的 Streamlit 主入口。

开发体验可在项目根目录执行 `npm --prefix frontend ci`、`npm --prefix frontend run build`，然后运行 `.venv\Scripts\python.exe scripts\run_workspace.py --no-ocr`。访问终端输出的本地地址；访问密钥位于终端提示的本地文件，请勿提交或分享。

## 本地运行

Python 3.10+。在仓库根目录执行以下 Windows CMD 命令；不需要激活虚拟环境。

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
if not exist .env copy .env.example .env
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

打开 <http://127.0.0.1:8502>。如果已有虚拟环境，直接执行最后一条命令。端口占用时更换端口，不必结束其他服务。

### 模型配置

已有 Ollama 模型时，在本地 `.env` 配置：

```env
LLM_BACKEND=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
```

Qwen 用于生成与行动决策；bge-m3 用于可选语义向量检索，两者不能互相替代。模型和密钥不随项目分发；下载模型须由操作者主动执行。

没有模型时可用 `LLM_BACKEND=offline` 检查界面与固定工作流，但离线模板不能完成 Agent 决策。

### Agent 测试

进入“AI 助手”，选择“受控检索 Agent”、Qwen2.5:7b 和“原文证据”。查看来源及 Agent Trace，确认实际检索和停止原因，再测试综合生成。

- 默认仍为固定 Workflow。
- Agent 最多检索三次，受步骤、估算 Token、超时和重复查询限制。
- Agent 即使选择摘录回答，也会调用行动决策模型。
- Agent 与目录库生成先做引用检查，再分块输出；legacy Workflow 保留实时生成流。
- 本机 7B 的两例检索/澄清冒烟通过；综合生成另一次触发支持度回退，尚无独立生成质量结论。

详见 [Agent 协议与使用](docs/RETRIEVAL_AGENT.md)。

## API

```bat
.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 <http://127.0.0.1:8000/docs>，可切换简中和英文说明。

| 接口 | 用途 |
|---|---|
| GET /healthz、/readyz | 存活与就绪检查 |
| POST /v1/assistant/reply | 完整 JSON 回答 |
| POST /v1/assistant/stream | SSE token / done / error |
| POST /v1/knowledge/graph | 有来源支持的实体共现路径 |

请求示例：

```json
{
  "question": "比较 Semaphore 并发限制和 RPM 限流，并引用证据",
  "execution_mode": "agent",
  "knowledge_source": "catalog",
  "kb_id": "python-official",
  "answer_mode": "extractive",
  "max_steps": 8,
  "max_estimated_tokens": 12000
}
```

先按 [目录库操作](docs/KNOWLEDGE_BASE.md) 导入资料。设置 `PERSONAX_API_TOKEN` 后，助手请求必须携带 Bearer Token。API 不提供发布接口；当前按单 Worker 运行，不能将线程或进程内限流当作分布式配额。

## 内容生成与文档导入

```bat
.venv\Scripts\python.exe main.py generate --topic "异步编程入门"
.venv\Scripts\python.exe main.py generate --topic "异步编程入门" --engine langgraph
.venv\Scripts\python.exe main.py knowledge ingest --file rag.md --license project-authored-demo
.venv\Scripts\python.exe main.py knowledge list
.venv\Scripts\python.exe main.py knowledge search --query "BM25 与 RRF"
```

生成命令默认 DryRun。真实发布需另外安装发布依赖、提供本机登录态并通过安全锁与人工确认；登录态不得提交。审批入口差异见 [架构说明](docs/PROJECT_TECHNICAL_WHITEPAPER.md)。

## Docker

```sh
docker compose up --build -d --wait
docker compose --profile api up --build -d api --wait
docker compose down
```

默认使用离线后端，端口仅绑定本机。连接宿主机 Ollama 时使用 Compose 的 `PERSONAX_OLLAMA_BASE_URL` 配置。数据保存在命名卷；不要通过删除卷来“重启”服务。

原镜像验证属于 2.8 基线；本轮目录库、Agent 和文档清理未重新验收镜像，不能将宿主机测试结果等同于新容器验证。

## 验证

```bat
.venv\Scripts\python.exe scripts/release_check.py
.venv\Scripts\python.exe scripts/check_privacy.py --strict
```

当前测试与测量口径见 [验证记录](docs/TEST_RESULTS.md)。真实模型冒烟是手动检查，不在离线门禁中自动调用。

## 文档

- [架构与技术取舍](docs/PROJECT_TECHNICAL_WHITEPAPER.md)
- [目录库操作](docs/KNOWLEDGE_BASE.md) · [混合检索配置](docs/RAG混合检索使用说明.md)
- [Agent 协议](docs/RETRIEVAL_AGENT.md) · [异步与限流](docs/ASYNC_ARCHITECTURE.md)
- [SSE 取消](docs/SSE_DISCONNECT_HANDLING.md) · [架构图](docs/知识图谱.md)
- [验证记录](docs/TEST_RESULTS.md) · [更新日志](docs/CHANGELOG.md)
- [安全政策](SECURITY.md) · [开发规范](AGENTS.md) · [后续计划](PLAN.md)

## 边界

适用于个人或受控单机环境，不提供完整租户隔离、高可用或公网生产保障。相关性阈值与词法支持度检查不是语义正确性证明；平台合规词表也不能代替平台审核。

简历、截图、凭据、会话和本地反馈不属于公开交付。检查不自动清理 Git 历史，也不覆盖图片 OCR 或远端存储。

## 作者与联系

作者：李卓

项目交流：3154433950@qq.com

欢迎通过 GitHub Issue 反馈使用问题。
