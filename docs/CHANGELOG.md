# 更新日志（Changelog）

## v2.1.0（2026-09-16）—— LangChain 工具标准化、LangGraph 真实编排与合规加固

### ✨ Agent 与 LangChain 生态
- AI 助手默认用 LangChain Core `StructuredTool` 调用只读知识检索，工具仍强制通过 Pydantic Schema、Harness 白名单/限流和 AuditLog
- 修复原 `core/graph.py` 仅有骨架、`router` 不执行 Skill 的问题
- 新增可运行 `LangGraphOrchestrator`：`StateGraph + RunnableLambda + MemorySaver`，风格校验失败时有界重试
- CLI 新增 `generate --engine langgraph`；Streamlit 生成页新增 Python / LangGraph 引擎选择
- 图节点全部复用既有 Skill 与 Harness，不绕过权限、限流和审计

### 🛡️ 合规与安全
- 修复合规配置中正则变体被错误转义的问题，“最好/最强/第一推荐”等现可正确拦截
- 自定义词条为无效正则时回退字面匹配，保持 fail-safe
- 新增 `scripts/check_content_compliance.py`，可在提交/发布前批量扫描 `content_bank`
- LangGraph 的完成节点不再写入虚假 `published=true`，仅记录 `graph_complete`；真实发布仍归 Publisher 与安全锁

### 🧪 验证
- LangChain StructuredTool、LangGraph Skill/Harness 执行、checkpoint 语义、合规正则和批量扫描均有回归测试
- 本版未进行小红书真实发布，仅执行 DryRun 和本地合规验收

---

## v1.1.1（2026-09-07）—— 发布前强制校验 AI 声明

### 🛡️ 合规加固
- **AI 声明强制校验**：`_mark_ai_generated()` 改为返回布尔值 —— 点击「笔记含AI合成内容」后**回读页面确认声明已选中/生效**（`aria-checked` / `aria-selected`、`active/selected/checked/chosen` 类、或选项内勾选子元素），最多重试 3 次
- **未确认则中止发布**：新增 `AIDeclarationUnconfirmed` 异常；`ai_generated=True` 时若无法确认声明生效，**抛异常中止，不再点击发布按钮**（防止发出未标识 AI 内容被限流/违规）
- `publish()` 将 `AIDeclarationUnconfirmed` 按「不可重试」处理：立即返回失败 + 截图 `ai_declaration_unconfirmed`，避免反复重开浏览器

### 🐛 修复
- AI 生成声明「点了就算成功」→ 改为「点击 + 回读校验 + 重试」，确认不到就不发
- 落实 v1.1.0 已知限制「真实发布需手动验证 AI 标注实际效果」：现在发布前自动校验

### 📌 已知限制
- 声明选中状态的判定为**启发式**（依赖平台 DOM），平台改版后可能误拦；策略偏严 —— **宁可不发，也不发未标识 AI 的内容**

---

## v1.1.0（2026-08-30）—— 发布质量与合规增强 + 量化实测

### ✨ 新功能

**真实发布安全锁**
- 新增 `XHS_REAL_PUBLISH_ENABLED`（默认 0 关闭）：生成/发布/调度三条路径接入，防止本地调试、账号受限期间误触发真实发布
- 命令行 `--keep-open-on-failure`：有头模式失败时保留浏览器窗口（关闭才结束），便于排查
- GUI 发布按钮同样受安全锁控制（未开启时禁用）

**发布质量**
- 发布按钮像素定位：截图宿主区域、扫描小红书品牌红色块质心，取代硬编码坐标，自适应按钮实际位置/窗口缩放
- 正文规范化：发布前清理游离的 `#话题` 行 + 压连续空行（规避小红书「不支持连续空行输入」导致正文被截断/吞字）
- 权限弹窗处理：`new_context` 拒绝定位权限 + 关闭「了解你的位置」等权限浮层

**合规增强**
- AI 生成标注：发布时自动勾选「笔记含AI合成内容」声明（对齐小红书社区公约 2.0 + 国家《人工智能生成合成内容标识办法》）
- 社区规范拦截检测：命中「违反社区规范禁止发笔记」时明确报错，不再误报成功
- 合规引擎命中带位置信息（title/body/tags + 命中原文 + 建议替换词）

**可视化工作台**
- 合规命中 →「📍 定位」：就地展开编辑面板，命中词 `<mark>` 高亮，改完保存写回草稿
- 保存后「修改前 → 修改后」diff 高亮（红删绿增）
- 生成后提示「发布时将标注内容由 AI 生成」

**量化实测与文档**
- 用量埋点：`core/usage.py` 自动记录 llm_call / gen / publish 三类事件 → `logs/usage.jsonl`
- 实测数据：5 次本地生成 + 真实发布留痕（单篇 6.9s / ≈1150 token / 干跑 5/5 / 真发 3 次）
- 文档：`docs/实测数据.md`（含真实发布案例 + 联网开关差异）、`docs/技术说明.json`、`docs/实测数据.json`

### 🐛 修复
- 「内容被社区规范拦截」被误判为发布成功 → 新增检测并明确报错
- AI 生成未标注会被限流 → 发布时自动勾选 AI 内容声明
- 话题标签混入正文 → 联想失败即移除 + 发布前清理游离 `#`
- 正文与生成不一致 → 压连续空行，规避「不支持连续空行输入」
- 「了解你的位置」权限弹窗遮挡编辑器 → 拒绝定位权限
- Streamlit widget 与 Session State 冲突（`e_body_3` 警告）→ 规范 `gen_ts` 刷新
- bind 计数补齐：合规引擎测试 52→55

### 📌 已知限制
- AI 生成声明勾选项文案以当前页面为准，页面改版后需复核选择器
- 真实发布需手动跑一次验证 AI 标注/正文规范化实际效果
- 其余同 v1.0.0（话题芯片、向量库、单账号、本地模型质量）

---

## v1.0.0（2026-08-29）—— 首个正式版

### ✨ 新功能

**内容生成**
- LLM 双后端 + 模型切换：DeepSeek（云端）/ 本地 Ollama（qwen2.5:3b、7b，免费）；未配 Key 自动探测本地，无可用则离线模板，永不报错
- 多人格系统（6 个人格：小鹿学姐 / 干货知识风 / 种草测评风 / 旅行探店风 / 情感共鸣风 / 职场进阶风）
  - 人格含 语气/习惯/开头风格/互动引导/标题风格/适用主题/风格示例
  - **按主题自动推荐人格**，可手动切换 / 一键回到推荐
- 提示词资产化：`config/prompts.yaml` 热更新（改文案不动代码）
- RAG 知识库：`knowledge/*.md`（front-matter 结构化）+ 中文 bigram 主题加权检索 + 相关性门槛防跑题
- 合规引擎：广告法绝对化用语 / 医疗金融承诺 / 导流话术，发布前自动拦截
- 内容清洗：去 Markdown 标记、去组合 emoji（①⃣）、限 emoji 数量、去「段一」标签

**真实发布**
- 上传图文模式（小红书新版发布页默认视频页签，自动切换）
- 闭合 Shadow DOM 发布按钮自动化（坐标定位点击）
- 真实成功判定：URL `published=true` / 成功提示 / 真实链接，无证据不报成功
- 扫码风控处理：有头模式自动等待 App 扫码（120s），扫码完成自动继续
- 登录态信任标记回写（降低「新设备+扫码」触发频率）
- 商用管控：发布限流 / 幂等防重复 / 人工审批 / 审计留痕 / 登录态隔离

**多模态 · 封面生成**
- 5 种本地海报风格：premium（高级暗调）/ minimal（留白极简）/ gradient / split / card
- **描述驱动**：用户描述封面（如「粉色渐变 可爱风」）→ 自动解析风格/配色生成
- AI 背景预留：配 SiliconFlow Key 后自动 AI 背景 + 标题叠加，无 Key 回退海报

**联网增强**
- 生成前抓热点/参考：Bing（免 Key）/ 博查 / Tavily
- 相关性过滤（结果须含主题片段才注入），失败自动跳过

**可视化工作台（Streamlit 5 页）**
- 生成编辑 / 内容库定时 / 知识库 / 设置（后端·模型·人格·联网·封面）/ 状态日志
- 未配 Key 清晰提示；主题占位输入；封面界面预览

**工程与运维**
- 52 项 pytest 全绿
- 安全校验：`scripts/check_secrets.py` + git pre-commit 钩子
- CLI：generate / publish / schedule / login / notes / probe / debug-selectors / personas / eval
- 文档：README / 开发记录 / 知识图谱（MD + Word）/ 本变更日志

### 🐛 修复
- 早期「发布成功」误报 → 严格证据判定（published=true）
- 长文编辑器无发布按钮 → 改走上传图文模式
- 闭合 Shadow DOM 按钮点不到 → 像素定位坐标点击
- 隐藏文件输入框 → `state=attached` + `set_input_files`
- pandas/numpy 不兼容崩溃 → 无 pandas 渲染 + 升级
- Streamlit session_state 时序错误 → on_click 回调
- 3b 模型 emoji 垃圾 → 字符清洗 + 提示词约束 + 升级 7b
- 标题被人格示例带偏 → 标题用精简人格
- 必应检索跑题 → 主题相关性过滤

### 📌 已知限制
- 话题「话题芯片」暂未自动添加（标签以 #文本 留正文，可在 App 补加）
- VectorStore 为内存实现（可换 Milvus/Qdrant）
- 单账号设计（多账号/并发为后续方向）
- 本地小模型质量有限（建议配 DeepSeek Key 或升级模型）
