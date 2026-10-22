# PersonaX 开发规范

修改前先阅读 SPEC.md。新增模块先定义数据契约，提交前执行测试与安全检查。

## 核心原则
**规范在前，编码在后。** 不允许"先写代码再补注释/规范"——每新增一个 Skill / 模块，先定义契约（manifest + Schema），再实现。

## 分层与依赖规则（硬约束）
```
接入层 (main.py / API)
   ↓ 只能调
编排层 (orchestrator.py / graph.py)
   ↓ 只能调
能力层 (skills/ / rag.py / llm.py)
   ↓ 只能调
基础设施 (types.py / registry.py / harness.py)
```
- **禁止反向依赖**：`skills/` 不得 import `orchestrator`；`core/llm.py` 不得 import `skills`。
- **跨层通信使用 types.py 对应契约**：Skill 使用 `Draft` / `SkillInput` / `ExecutionContext`；助手与工具使用各自请求/响应类型，checkpoint 序列化场景须注明。
- **依赖注入**：Skill 所需的 persona / ctx 通过 `SkillInput.context` 传入，**禁止全局单例读取配置**。

## Skill 开发规范（强制）
1. 继承 `core.registry.Skill`，实现 `run(inp: SkillInput) -> SkillOutput`。
2. **必须用 `@register` 装饰器注册**，文件名即模块名，放在 `skills/` 下，被 `skills/__init__.py` 显式 import。
3. 必须填：`name`、`description`（一句人话，用于路由）、`triggers`（关键词列表）、`popularity`（0~1）。
4. 输出**只改 `inp.draft`**，返回 `SkillOutput(draft=..., notes=[...])`；禁止副作用（发请求/写文件）——副作用归 `publishers/` 或 `tools/`。
5. 每个 Skill **必须有对应单元测试**（`tests/`），覆盖正常 + forbidden 命中。

## Persona / 风格
- 人格配置**只在 `config/persona.yaml`**，代码里不要硬编码"小鹿学姐"等字样。
- 风格硬约束（`forbidden` / 句长）由 `StyleEnforcer` 统一校验；**业务 Skill 不得自行再做文本清洗**。
- 模型调用使用 `core/llm.py` 的同步或异步入口，禁止在 Skill 或编排器中自行构造客户端。

## 管控与合规
- 所有 Skill 执行**必须经过 Harness 拦截**（allow / quota / sensitive_tool 审批），不允许绕过。
- 发布类 Skill（`xhs_publish` 等）默认 `require_approval=true`，必须人工二次确认。
- 审计日志（`AuditLog`）**每条关键动作都要写**，禁止吞异常。

## RAG
- 索引/查询逻辑集中在 `core/rag.py`；新增检索策略**扩展 `RAGPipeline` 方法**，不改 VectorStore 接口。
- Chunk 必须带 `metadata`（来源、主题），便于权限过滤与溯源。

## 提交前自检（Checklist）
- [ ] `pytest tests/` 全绿
- [ ] 新增 Skill 已 `@register` 且 import 到 `skills/__init__.py`
- [ ] 无跨层裸 dict、无全局配置读取
- [ ] LLM 调用走 `core/llm.complete`
- [ ] 有对应的 `eval_set` 用例 + 打分结果
- [ ] `python main.py generate --topic xxx` DryRun 跑通
