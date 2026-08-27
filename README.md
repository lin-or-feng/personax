# PersonaX 🍃 小红书内容生成与自动发布 Agent

> 一个**真实可用**的小红书内容 Agent：LLM 多人格写作 × Skill 系统 × 合规管控 × Playwright 真实发布 × 定时调度 × 可视化工作台。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-green) ![Playwright](https://img.shields.io/badge/UI%20Automation-Playwright-orange) ![Tests](https://img.shields.io/badge/Tests-38%20passed-brightgreen)

## ✨ 亮点（Highlights）

- **LLM 内容生成**：接入 DeepSeek，提示词资产化（`config/prompts.yaml` 热更新，改文案不动代码）
- **多人格系统**：`config/personas.yaml` 人格库，生成时 `--persona <名字>` 选谁用谁写（内置 4 个人格）
- **RAG 知识库增强**：`knowledge/*.md` 喂优质范例 → 中文 bigram 主题加权检索 + 相关性门槛（不跑题）
- **商用合规引擎**：广告法/医疗金融承诺/导流词表，发布前自动拦截违规内容
- **真实发布（已实测真发成功）**：Playwright 驱动创作者平台「上传图文」，攻克闭合 Shadow DOM 发布按钮、隐藏文件上传、平台改版容错；以 URL `published=true` 判定真成功
- **定时自动发布**：`content_bank` 稿件库 + 到期自动发 + `publish_log.json` 留痕 + 幂等防重复
- **可视化工作台**：Streamlit 5 页（生成编辑 / 内容库定时 / 知识库 / 设置 / 状态日志）
- **工程化**：三层解耦、跨层数据契约、Skill 注册路由、审计留痕、38 项 pytest 全绿

## 🏗️ 架构

```
接入层     main.py (CLI) + app.py (Streamlit 可视化)
   ↓
编排层     orchestrator.py（Skill 链 + Harness 拦截 + 风格闭环）+ graph.py（LangGraph，可选）
   ↓
能力层     skills/（标题/正文/标签/封面/就绪门禁）+ rag.py + llm.py（DeepSeek）
   ↓
基础设施   types.py(契约) registry.py(路由) harness.py(规则引擎) style.py
           compliance.py(合规) persona.py(多人格) prompts.py(提示词资产)
   ↓
执行层     publishers/（xhs.py Playwright 真发 / scheduler.py 定时调度）
```

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

### 7. 测试与评估

```bash
pytest tests/            # 38 项单测
python main.py eval      # 内容质量打分 → eval_results.csv
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
│   └── compliance.yaml     # 合规词表（广告法/医疗金融/导流）
├── core/                   # 编排层 + 基础设施
│   ├── orchestrator.py     # 纯 Python 编排器
│   ├── graph.py            # LangGraph 图编排（可选）
│   ├── harness.py          # 规则引擎（限流/审批/审计）
│   ├── compliance.py       # 合规引擎
│   ├── rag.py              # RAG（中文 bigram + 主题加权 + 相关性门槛）
│   ├── persona.py          # 多人格库
│   ├── prompts.py          # 提示词资产加载
│   └── llm.py              # DeepSeek 客户端（重试/超时/惰性依赖）
├── skills/                 # Skill 系统（@register + 路由）
├── publishers/             # 发布层（Playwright 真发 / 定时调度）
├── knowledge/              # RAG 知识库（*.md 带 front-matter）
├── content_bank/           # 定时稿件库（*.json）
├── eval/                   # 评估闭环
└── tests/                  # pytest（38 项）
```

## 🎛️ 调优方向（怎么让内容更好）

| 杠杆 | 位置 | 说明 |
|---|---|---|
| ① 真实 LLM | `.env` 配 Key | 最大提升：模板文 → DeepSeek 现写 |
| ② 提示词资产 | `config/prompts.yaml` | 改生成要求，热更新不用改代码 |
| ③ 知识库 RAG | `knowledge/*.md` | 喂同主题优质范例，生成时自动召回学习结构与语气 |
| ④ 多人格 | `config/personas.yaml` | 不同语气人设，`--persona` 切换 |
| ⑤ 发布稳定性 | `publishers/xhs.py` | 平台改版用 `python main.py probe --mode tuwen` 诊断 |

**发布稳定性诊断**（平台改版后元素找不到时）：
```bash
python main.py debug-selectors --browser msedge
python main.py probe --mode tuwen --upload assets/note_cover.png --browser msedge
```

## 🛡️ 安全与合规

- Key 存 `.env`（gitignore），登录态 `storage_state.json` 不提交
- 发布前人工审批（`--yes` 才跳过）+ 每分钟发布限速 + **幂等**防重复发
- 合规引擎自动拦截广告法绝对化用语 / 医疗金融承诺 / 导流话术
- 审计留痕：`publish_log.json` + 运行期 AuditLog
- ⚠️ 自动化发布请遵守小红书平台规则，控制频率，谨慎使用

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

## ⚠️ 已知限制（Roadmap）

- 话题「话题芯片」暂未自动添加（网页编辑器话题面板交互复杂，标签以 `#文本` 留正文，可在 App 补加）
- VectorStore 为内存实现（接口已抽象，可换 Milvus/Qdrant）
- LangGraph 图编排为可选路径（纯 Python 编排器已可用）
- 单账号设计（多账号/并发为后续方向）

## 🧰 技术栈

Python 3.10+ · DeepSeek (OpenAI SDK) · Playwright · Pydantic · Streamlit · LangGraph(可选) · PyYAML · pytest

## 📄 License

MIT（请按需修改）
