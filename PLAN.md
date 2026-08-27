# PersonaX 项目搭建计划

## 目标
基于商用框架文档（LangGraph × Harness × Skill），搭建可运行的最小工程：
- LangGraph 状态机编排（checkpoint + human-in-the-loop）
- Skill 注册中心 + 多信号路由（对齐文档 Skill manifest 标准）
- RAG：四索引 + 四查询 + RRF（可运行骨架）
- Harness 规则引擎（allow/deny/quota/approval）
- 评测体系（eval set + scorer）
- 秋招讲述主线

## 目录结构
```
personax/
├── pyproject.toml
├── README.md
├── AGENTS.md              # 给 dsh/AI 的编码规范
├── SPEC.md                # 架构硬约束
├── config/
│   └── persona.yaml
├── core/
│   ├── __init__.py
│   ├── types.py           # Draft, SkillContext, ExecutionContext
│   ├── llm.py             # DeepSeek (openai-compatible)
│   ├── registry.py        # SkillRegistry + @register
│   ├── style.py           # StyleEnforcer
│   ├── graph.py           # LangGraph StateGraph
│   ├── harness.py         # 规则引擎
│   └── rag.py             # 四索引 + 四查询 + RRF
├── skills/
│   ├── __init__.py
│   └── content.py         # Title/Tag/Body/Cover
├── publishers/
│   ├── base.py
│   └── xhs.py             # Playwright (optional)
├── eval/
│   ├── eval_set.json
│   └── scorer.py
├── tests/
│   └── test_pipeline.py
└── main.py
```

## 执行顺序
1. 配置文件 + 依赖 (pyproject.toml)
2. core/types.py - 数据契约
3. core/llm.py - 真实 LLM 调用
4. core/registry.py + skills/ - Skill 系统
5. core/style.py - 风格约束
6. core/rag.py - RAG 骨架
7. core/harness.py - 规则引擎
8. core/graph.py - LangGraph 编排
9. orchestrator + main.py - 串起来
10. eval + tests
11. README + 验证

## 验证
- `pytest tests/` 全绿
- `python main.py --topic "秋招穿搭"` 跑通完整链路
- `python eval/scorer.py` 输出 CSV
