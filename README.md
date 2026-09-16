# PersonaX 2.0 🍃 内容生成、知识问答与安全发布 Agent

> 一个**真实可用**的小红书内容 Agent：LLM 多人格写作 × Skill 系统 × 合规管控 × Playwright 真实发布 × 定时调度 × 可视化工作台。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-green) ![Playwright](https://img.shields.io/badge/UI%20Automation-Playwright-orange) ![Tests](https://img.shields.io/badge/Tests-79%20passed-brightgreen)

## ✨ 亮点（Highlights）

- **LLM 内容生成**：接入 DeepSeek，提示词资产化（`config/prompts.yaml` 热更新，改文案不动代码）
- **有状态 AI 助手**：Streamlit 原生对话界面，按需检索、可追溯引用、Agent Trace、SQLite Checkpoint
- **多人格系统**：`config/personas.yaml` 人格库，生成时 `--persona <名字>` 选谁用谁写（内置 4 个人格）
- **RAG 混合检索**：BM25 + dense embedding 精确 kNN 双路召回，RRF 融合；支持 query rewriting / HyDE、cross-encoder 重排与相关性门控
- **商用合规引擎**：广告法/医疗金融承诺/导流词表，发布前自动拦截违规内容
- **真实发布（已实测真发成功）**：Playwright 驱动创作者平台「上传图文」，攻克闭合 Shadow DOM 发布按钮、隐藏文件上传、平台改版容错；以 URL `published=true` 判定真成功
- **定时自动发布**：`content_bank` 稿件库 + 到期自动发 + `publish_log.json` 留痕 + 幂等防重复
- **可视化工作台**：Streamlit 6 页（生成编辑 / AI 助手 / 内容库定时 / 知识库 / 设置 / 状态日志）
- **工程化**：三层解耦、跨层数据契约、Skill 注册路由、审计留痕、79 项 pytest 全绿

## 🆕 PersonaX 2.0：八个 Agent 模块的项目化落地

2.0 没有把所有概念堆成多个重型服务，而是围绕本地 3B/7B 模型做了一条可运行、可解释、可测试的对话链路：

```text
用户问题
  → Supervisor 路由（直接回答 / 知识检索）
  → Retriever Worker（BM25 + dense kNN + RRF + min_score）
  → Answerer Worker（一次 LLM 调用，结合最近 8 条消息）
  → Reviewer Worker（非空与引用检查）
  → SQLite Checkpoint + AuditLog + usage trace
```

| 教学模块 | PersonaX 2.0 实现 | 边界说明 |
|---|---|---|
| 1. Agent 认知与选型 | 采用显式、最多 4 步的 Agent loop；简单问候跳过检索 | 本地场景不为框架而框架 |
| 2. LLM 原语 | 所有生成统一走 `core.llm.complete()`；结构化输入输出、重试、失败降级；助手提示词在 `config/assistant_prompts.yaml` | 默认只调用一次 LLM，控制延迟和显存压力 |
| 3. 状态机 | `AssistantRequest/Response`、显式 Trace、SQLite Checkpointer、最大步数 | 助手状态机用轻量 Python 编排；内容流水线仍保留可选 LangGraph |
| 4. Agentic RAG | 按需检索、混合召回、门控、检索失败降级、回答引用来源 | 关闭“本地知识库”后可强制直答 |
| 5. Multi-Agent | Supervisor + Retriever/Answerer/Reviewer 职责隔离 | 是可单测的逻辑 Worker，不冒充多个独立模型并行 |
| 6. MCP 与工具工程化 | `KnowledgeSearchTool` 有 Pydantic 输入/输出、工具 Manifest、JSON Schema、Harness 管控与审计 | 当前是 **MCP-ready 契约**，尚未启动独立 MCP Server |
| 7. Eval & Observability | 8 条离线助手回归集；路由、引用、步数、回答率；UI 展示逐步 Trace | 该评测不代表真实 LLM 回答质量，后续再扩充人工/LLM-as-Judge 集合 |
| 8. 生产化 | 上下文裁剪、限流、最大步数、故障降级、本地 checkpoint、后端切换时隔离客户端 | 未声称已完成高并发、Redis、Docker 或线上 SLA |

助手严格只读：它能查询知识库、解释项目和给出建议，但没有真实发布工具；发布仍必须经过现有人工确认与安全锁。

## 🏗️ 架构

```
接入层     main.py (CLI) + app.py (Streamlit 可视化)
   ↓
编排层     orchestrator.py（内容 Skill 链）+ assistant.py（对话 Supervisor-Worker）
           + graph.py（LangGraph，可选）
   ↓
能力层     skills/（标题/正文/标签/封面/就绪门禁）+ rag.py + llm.py（DeepSeek）
   ↓
基础设施   types.py(契约) registry.py(路由) harness.py(规则引擎) assistant_tools.py
           style.py
           compliance.py(合规) persona.py(多人格) prompts.py(提示词资产)
   ↓
执行层     publishers/（xhs.py Playwright 真发 / scheduler.py 定时调度）
```

## 🔎 RAG 混合检索改进

当前检索链路：

```text
用户主题
  → Query Rewriting（默认规则，可选 LLM）/ 可选 HyDE
  → BM25 关键词召回 + dense embedding 精确 kNN 召回
  → RRF 融合不同分数量纲的排名
  → 可选 Cross-Encoder 对候选文档重排
  → min_score 相关性门控
  → 相关知识注入内容生成提示词
```

主要改进：

- **BM25 稀疏检索**：保留专有名词、岗位名、学校名等精确关键词信号；零分文档不会进入 RRF。
- **dense kNN 语义检索**：用同一 embedding 模型编码查询和知识片段，遍历全部向量计算余弦相似度并取 Top-K。
- **RRF 排名融合**：只使用每路结果的名次进行融合，避免直接混合 BM25 分数与 cosine 分数。
- **查询增强**：短主题默认使用低延迟规则改写；复杂问题可通过现有 LLM 后端生成多查询或 HyDE 假设答案。
- **Cross-Encoder 重排**：只对 RRF 召回的少量候选做 query-document 联合打分，降低全库计算成本。
- **相关性门控**：`min_score` 过滤低相关知识，避免无关范例污染生成结果。
- **可观测性**：每次召回将查询变体、embedding 后端、kNN 模式、融合与重排信息写入 `draft.metadata.rag_trace`。
- **持久化嵌入缓存**：非 hashing 文档向量写入 D 盘 `.rag_cache/embeddings.sqlite3`，按模型和文本哈希复用；用户查询不入库，避免无界增长。
- **参考/范例分流**：公开百科标记为 `reference`，只用于概念和事实；手工收藏的优质笔记标记为 `example`，才用于学习结构与语气。

为什么现阶段使用精确 kNN，而不是 ANN：当前知识库规模较小，精确 kNN 实现直观、结果可复现且不会损失召回率。等知识库增长到数万以上 chunk，并通过性能测试确认向量遍历成为瓶颈后，再替换为 FAISS/HNSW 等 ANN 索引。

### Ollama 模型分工

内容生成模型和 embedding 模型用途不同，需要分别安装：

```powershell
# 内容生成、LLM Query Rewriting / HyDE
ollama pull qwen2.5:7b

# dense 语义检索
ollama pull bge-m3
```

### Windows 将 Ollama 模型放到 D 盘

模型默认位于 `%USERPROFILE%\.ollama\models`。C 盘空间紧张时，推荐先退出托盘中的 Ollama，再设置用户环境变量并重新启动：

```powershell
New-Item -ItemType Directory -Force D:\OllamaModels
setx OLLAMA_MODELS "D:\OllamaModels"
```

如果系统策略不允许写环境变量，也可在确认默认模型目录不存在后建立目录联接：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ollama"
New-Item -ItemType Junction `
  -Path "$env:USERPROFILE\.ollama\models" `
  -Target "D:\OllamaModels"
```

当前开发环境采用第二种方式：`%USERPROFILE%\.ollama\models` 只是指向 `D:\OllamaModels` 的联接，不在 C 盘重复保存模型。`bge-m3` 下载约 1.2GB，本机加载实测约占 664MB GPU 显存；实际占用会随 Ollama 版本、量化和运行设备变化。

`.env` 示例：

```env
LLM_BACKEND=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1

RAG_EMBEDDING_BACKEND=ollama
RAG_EMBEDDING_MODEL=bge-m3
RAG_EMBEDDING_CACHE=1
RAG_QUERY_ENHANCER=rule
RAG_ENABLE_HYDE=0
RAG_RERANKER=none
```

### 公开中文知识包（小体积）

项目提供定向导入器，从 Wikimedia 官方 API 获取武汉、校园、求职、Agent/RAG 和内容营销主题。每篇 Markdown 都保留原始链接、获取日期和许可信息，不会下载数 GB 的全量 dump。

```powershell
& .\.venv\Scripts\python.exe scripts\import_public_knowledge.py --embed
```

首次 `--embed` 会调用本地 `bge-m3`；之后只为新增或变更的 chunk 重算向量。删除 `.rag_cache/embeddings.sqlite3` 可完全重建缓存。

联网搜索与本地知识库互不改写：本地 RAG 提供稳定可复现的背景，联网搜索仅在生成当次补充时效信息。搜索摘要不会自动写入向量库。

默认 `hashing` embedding 仅用于无模型环境下的离线测试，不能称为真实语义向量模型。启用 Cross-Encoder 时需安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rag]"
```

检索回归评测：

```powershell
.\.venv\Scripts\python.exe -m eval.rag_scorer --top-k 1
```

评测会输出 `Recall@K`、`MRR` 和 `rag_trace`。仓库自带 6 条样例只用于冒烟回归；对外描述性能前，应扩充到至少 50～100 条人工标注查询，并做 BM25、dense kNN、RRF、RRF + reranker 消融对比。

## 🚀 快速开始

### 0. 环境准备（Python 3.10+）

```bash
git clone <你的仓库地址>
cd personax

# Windows 一键安装（自动装依赖 + Playwright + Chromium）
.\setup.ps1

# 或手动：
pip install -e .
pip install playwright
python -m playwright install chromium   # 不想下载可用系统浏览器 --browser msedge
```

### 1. 配置 DeepSeek Key（不配也能跑，走离线模板）

创建 `.env`（参考 `.env.example`，已被 .gitignore 忽略）：
```
DEEPSEEK_API_KEY=sk-你的key
```

### 2. 登录小红书（一次性，导出登录态）

```bash
python main.py login --browser msedge    # 弹浏览器扫码登录，生成 storage_state.json
```

### 3. 生成内容（干跑，不真发）

```bash
python main.py generate --topic "秋招穿搭"
python main.py generate --topic "考研英语" --persona 干货知识风   # 按人格生成
```

### 4. 真实发布

```bash
python main.py publish --draft content_bank/example.json --real --browser msedge
```

发布前会人工确认（输入 `y`）；无人值守加 `--yes`。

### 5. 定时自动发布

```bash
# 稿件放 content_bank/*.json（scheduled_at 到期自动发）
python main.py schedule --run-once --real --yes --browser msedge
python main.py schedule --daemon --interval 60 --real --yes --browser msedge   # 常驻
```

### 6. 可视化工作台

```bash
pip install streamlit
python -m streamlit run app.py    # 打开 http://localhost:8501
```

Windows 也可以直接双击项目根目录的 `启动PersonaX可视化.cmd`，或使用桌面上的“PersonaX 可视化”快捷方式。入口会自动检查 Ollama、在独立后台进程中启动 Streamlit 并打开浏览器；命令窗口自动关闭不会停止页面，重复点击会复用已经运行的 8501 服务。启动失败时查看 `.tmp/start-ui.log` 和 `.tmp/streamlit-8501.stderr.log`。

进入左侧 **💬 AI 助手** 即可对话。默认使用本地 Ollama；可在页面上切换模型、开关本地知识库，并展开每条回答下方的“引用来源”和“Agent Trace”。助手不会调用发布功能。

### 7. 测试与评估

```bash
pytest tests/            # 79 项单测
python main.py eval      # 内容质量打分 → eval_results.csv
python -m eval.assistant_scorer  # AI 助手离线回归（不调用 Ollama）
python -m eval.rag_scorer --top-k 1  # RAG Recall@K / MRR
python main.py notes --browser msedge   # 核实真实发布的笔记
```

## 📂 项目结构

```
personax/
├── main.py                 # CLI 入口（generate/publish/schedule/login/notes/probe/personas/eval）
├── app.py                  # Streamlit 可视化工作台
├── setup.ps1               # Windows 一键安装
├── config/
│   ├── persona.yaml        # 默认人格 + 系统规则（harness/rag/generation）
│   ├── personas.yaml       # 人格库（多人格）
│   ├── prompts.yaml        # 提示词资产（标题/正文/标签/封面模板）
│   ├── assistant_prompts.yaml # AI 助手提示词资产
│   └── compliance.yaml     # 合规词表（广告法/医疗金融/导流）
├── core/                   # 编排层 + 基础设施
│   ├── orchestrator.py     # 纯 Python 编排器
│   ├── assistant.py        # 对话 Supervisor-Worker + Checkpointer
│   ├── assistant_tools.py  # 类型化检索工具 + MCP-ready Manifest
│   ├── graph.py            # LangGraph 图编排（可选）
│   ├── harness.py          # 规则引擎（限流/审批/审计）
│   ├── compliance.py       # 合规引擎
│   ├── rag.py              # RAG（BM25 + dense kNN + RRF + 可选重排）
│   ├── persona.py          # 多人格库
│   ├── prompts.py          # 提示词资产加载
│   └── llm.py              # DeepSeek 客户端（重试/超时/惰性依赖）
├── skills/                 # Skill 系统（@register + 路由）
├── publishers/             # 发布层（Playwright 真发 / 定时调度）
├── knowledge/              # RAG 知识库（*.md 带 front-matter）
├── content_bank/           # 定时稿件库（*.json）
├── eval/                   # 评估闭环
└── tests/                  # pytest（79 项）
```

## 🎛️ 调优方向（怎么让内容更好）

| 杠杆 | 位置 | 说明 |
|---|---|---|
| ① 真实 LLM | `.env` 配 Key | 最大提升：模板文 → DeepSeek 现写 |
| ② 提示词资产 | `config/prompts.yaml` | 改生成要求，热更新不用改代码 |
| ③ 知识库 RAG | `knowledge/*.md` | 混合召回同主题资料；默认离线 hashing，Ollama `bge-m3` 才是真实语义向量 |
| ④ 多人格 | `config/personas.yaml` | 不同语气人设，`--persona` 切换 |
| ⑤ 发布稳定性 | `publishers/xhs.py` | 平台改版用 `python main.py probe --mode tuwen` 诊断 |

**发布稳定性诊断**（平台改版后元素找不到时）：
```bash
python main.py debug-selectors --browser msedge
python main.py probe --mode tuwen --upload assets/note_cover.png --browser msedge
```

## 🛡️ 安全与合规

- Key 存 `.env`（gitignore），登录态 `storage_state.json` 不提交
- **真实发布安全锁**：`XHS_REAL_PUBLISH_ENABLED=1` 才允许真发（默认关闭，仅生成/预览/干跑）
- 发布前人工审批（`--yes` 才跳过）+ 每分钟发布限速 + **幂等**防重复发
- **AI 声明强制校验**：发布前自动勾选「笔记含AI合成内容」并**回读确认已生效**，确认不到就**中止发布**（不发出未标识 AI 内容）
- 合规引擎自动拦截广告法绝对化用语 / 医疗金融承诺 / 导流话术
- **对话助手只读**：工具清单仅包含本地知识检索，不暴露发布能力；检索片段作为不可信资料隔离注入提示词
- **会话级限流**：Streamlit 重跑时复用当前会话的 Harness，不会因每次点击重建对象而清空限额
- **外部链接白名单**：知识库来源仅保留 `http/https` 链接，其他协议不会在页面上生成可点击链接
- 审计留痕：`publish_log.json` + 运行期 AuditLog
- ⚠️ 自动化发布请遵守小红书平台规则，控制频率，谨慎使用

### 安全回归与验收

安全检查覆盖密钥/敏感文件泄漏、真实发布默认关闭、工具权限边界、会话限流、SQL 参数化、恶意知识片段隔离和外部链接协议校验。所有检查只使用本地模拟数据，**不登录、不调用小红书、不真实发布**。

```powershell
& .\.venv\Scripts\python.exe scripts\check_secrets.py --strict
& .\.venv\Scripts\python.exe -m pytest tests\test_security.py
& .\.venv\Scripts\python.exe -m pytest tests
```

本次完整的命令、环境、通过数和剩余边界记录在 [`docs/TEST_RESULTS.md`](docs/TEST_RESULTS.md)。

## 🔒 上传 GitHub 前安全校验

**每次 push 前跑一遍**，自动扫描密钥与敏感文件（.env / storage_state.json / 登录态 / Key 明文）：

```bash
python scripts/check_secrets.py             # 扫描；有 🔴 危险项会列出
python scripts/check_secrets.py --strict    # 发现危险项退出码 1（可挂 CI / pre-commit）
```

一键安装提交前钩子（每次 `git commit` 自动校验，危险项直接阻止提交）：

```bash
python scripts/install_hooks.py
```

> 校验规则与 `.gitignore` 同步：`.env`、`storage_state.json`、`logs/`、`publish_log.json` 等都会被识别为「已忽略、本地安全」，不会误报。
> ⚠️ **如果 Key 曾在任何渠道泄露过**（如聊天记录/截图），请到 [platform.deepseek.com](https://platform.deepseek.com) **删除重建**，新 Key 只放 `.env`。

## 💰 省钱方案（生成太花钱？）

每篇笔记会调 4 次 LLM（标题/正文/标签/封面），按性价比排序：

| 方案 | 成本 | 做法 | 质量 |
|---|---|---|---|
| **① 本地 Ollama（推荐）** | **0 元** | `.env` 加 `LLM_BACKEND=ollama`，先 `ollama pull qwen2.5:7b`；模型用 `LLM_MODEL=qwen2.5:7b` | 良好（本地 7B） |
| ② 云端便宜模型 | 极低 | 界面**别选 deepseek-reasoner**（贵 4-5 倍）；deepseek-chat 每篇约 1 分钱 | 好 |
| ③ 免费额度模型 | 0 元 | 智谱 GLM-4-Flash 免费：`.env` 设 `DEEPSEEK_BASE_URL` 换 OpenAI 兼容端点 | 中等 |
| ④ 省 token | — | 标题候选已 3 个；`generation.max_tokens` 可调小；已有内容的稿件不重复生成（调度器已做） | — |

> 已内置：`LLM_BACKEND=ollama` 自动走本地（无需 Key），默认 `qwen2.5:7b`（`LLM_MODEL` 可改），
> 输出上限自动降到 512 token；可视化界面模型下拉随后端切换。

## ⚠️ 已知限制（Roadmap）

- 话题「话题芯片」暂未自动添加（网页编辑器话题面板交互复杂，标签以 `#文本` 留正文，可在 App 补加）
- VectorStore 为内存实现（接口已抽象，可换 Milvus/Qdrant）
- LangGraph 图编排为可选路径（纯 Python 编排器已可用）
- 单账号设计（多账号/并发为后续方向）

## 🧰 技术栈

Python 3.10+ · DeepSeek (OpenAI SDK) · Playwright · Pydantic · Streamlit · LangGraph(可选) · PyYAML · pytest

## 📄 License

MIT（请按需修改）
