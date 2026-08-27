"""Orchestrator：编排层——串联 Skill 链 + Harness 拦截 + 风格校验闭环"""
from __future__ import annotations
import uuid
from .types import Draft, SkillInput, ExecutionContext
from .registry import route as route_skills, all_skills
from .style import StyleEnforcer
from .harness import Harness, RuleConfig, AuditLog
from .llm import build_system_prompt
from .prompts import load_prompts
from .rag import build_rag_from_dir


class Orchestrator:
    """内容生成工作流编排器（不依赖 LangGraph，纯 Python 可跑；graph.py 为其图版本）"""

    def __init__(self, persona: dict, harness: Harness | None = None):
        self.persona = persona
        self.harness = harness or Harness(RuleConfig(**persona.get("harness", {})))
        self.audit = self.harness.audit
        self.enforcer = StyleEnforcer(persona)

    def run(self, topic: str, user_id: str | None = None, skill_chain: list[str] | None = None) -> Draft:
        draft = Draft(topic=topic)
        # 提示词资产 + RAG 知识库（可热更新，缺省安全回退）
        prompts = load_prompts(self.persona.get("prompts_path", "config/prompts.yaml"))
        rag_pipe = build_rag_from_dir(self.persona.get("rag", {}).get("knowledge_dir", "knowledge"))
        # 只召回与主题足够贴近的范例（min_score），避免不相干知识跑题；缺省空列表
        rag_examples = (
            [c.text for c in rag_pipe.retrieve_relevant(topic, top_k=2, min_score=0.10)]
            if rag_pipe.store.chunks else []
        )
        ctx = ExecutionContext(
            run_id=str(uuid.uuid4())[:8],
            persona=self.persona,
            user_id=user_id,
            prompts=prompts,
            rag_examples=rag_examples,
        )
        chain = skill_chain or [name for name, _ in route_skills(topic, top_k=3)]

        for skill_name in chain:
            # ★ Harness 执行前拦截（allow + quota + 敏感工具审批）
            allowed, reason = self.harness.guard(skill_name, user_id)
            if not allowed:
                self.audit.write(event="skill_skipped", skill=skill_name, reason=reason)
                continue
            if self.harness.needs_approval(skill_name):
                self.audit.write(event="approval_required", skill=skill_name)
                # 演示环境默认批准；生产环境在此挂起等待人工确认
            skill = all_skills().get(skill_name)
            if skill is None:
                continue
            result = skill.run(SkillInput(draft=draft, context={
                "persona": self.persona,
                "prompts": prompts,
                "rag_examples": rag_examples,
            }))
            draft = result.draft
            self.audit.write(event="skill_done", skill=skill_name, notes=result.notes)

        # ★ 风格校验闭环：不通过则触发重写（最多重试；内容无变化则提前终止，避免空转）
        max_retries = self.persona.get("harness", {}).get("max_retries", 2)
        for attempt in range(max_retries + 1):
            report = self.enforcer.enforce(draft)
            ctx.checkpoint["style_report"] = report.model_dump()
            if report.ok:
                break
            self.audit.write(event="style_retry", attempt=attempt, issues=report.issues)
            if attempt >= max_retries:
                break
            # 重写正文（真实 LLM 场景会产出新文本；离线模板无变化则终止）
            before = draft.body
            writer = all_skills().get("body_writer")
            if writer is None:
                break
            result = writer.run(SkillInput(draft=draft, context={
                "persona": self.persona, "prompts": prompts, "rag_examples": rag_examples,
            }))
            draft = result.draft
            if draft.body == before:
                break

        ctx.checkpoint["final_draft"] = draft.model_dump()
        return draft
