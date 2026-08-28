# 更新日志（Changelog）

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
