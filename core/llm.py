"""LLM 客户端：DeepSeek（OpenAI 兼容协议）——商用健壮版

- 环境变量 DEEPSEEK_API_KEY 必填才走真实模型；未设置时返回占位文本（离线演示/测试）
- openai 库惰性导入：未安装时若设置了 Key，抛出带安装提示的 LLMError
- 真实调用：超时 + 指数退避重试（默认 3 次），仅对可重试错误（限流/超时/断连）重试
- 统一入口 core.llm.complete()，业务 Skill 禁止直接 new client
"""
from __future__ import annotations
import os
import time
from pathlib import Path
from typing import Optional


class LLMError(Exception):
    """LLM 调用最终失败（重试耗尽 / 依赖缺失 / 参数错误）"""


def _load_project_env() -> None:
    """任何入口都生效：自动读取项目根目录 .env（含 DEEPSEEK_API_KEY）"""
    try:
        from .envfile import load_env_file
        root = Path(__file__).resolve().parent.parent
        load_env_file(root / ".env")
    except Exception:  # noqa: BLE001 —— .env 读取失败不影响启动
        pass


_load_project_env()


_client: Optional[object] = None
_OpenAI = None
_RETRYABLE: tuple = ()
_OVERRIDES: dict = {}   # 运行时覆盖（可视化界面设置后端/模型/温度）
_OLLAMA_CHECK: dict = {"ts": 0.0, "ok": False}   # 本机 Ollama 可达性缓存（5s）


def configure(*, backend: str | None = None, model: str | None = None,
              temperature: float | None = None, max_tokens: int | None = None):
    """运行时覆盖生成参数（供可视化界面调用；不设置则用默认值）。

    backend: "ollama"（本地免费）/ "deepseek"（云端）/ "offline"（模板）
    """
    if backend is not None:
        _OVERRIDES["backend"] = backend.strip().lower()
    if model is not None:
        _OVERRIDES["model"] = model
    if temperature is not None:
        _OVERRIDES["temperature"] = temperature
    if max_tokens is not None:
        _OVERRIDES["max_tokens"] = max_tokens


def _ollama_reachable(timeout: float = 0.6) -> bool:
    """检测本机 Ollama 是否在运行（缓存 5 秒）"""
    import time as _t
    if _t.time() - _OLLAMA_CHECK["ts"] < 5:
        return _OLLAMA_CHECK["ok"]
    try:
        import socket
        s = socket.create_connection(("127.0.0.1", 11434), timeout=timeout)
        s.close()
        _OLLAMA_CHECK.update(ts=_t.time(), ok=True)
        return True
    except OSError:
        _OLLAMA_CHECK.update(ts=_t.time(), ok=False)
        return False


def _backend() -> str:
    """当前 LLM 后端（优先级）：
    1) 界面 configure(backend=...) 显式选择
    2) 环境变量 LLM_BACKEND
    3) 自动：有 DEEPSEEK_API_KEY → deepseek；否则本机 Ollama 在线 → ollama；否则 deepseek（走模板）
    """
    ov = _OVERRIDES.get("backend")
    if ov:
        return ov
    b = os.getenv("LLM_BACKEND", "").strip().lower()
    if b:
        return b
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "ollama" if _ollama_reachable() else "deepseek"


def _default_model() -> str:
    """按后端返回默认模型名（可被 persona.generation.model 或界面覆盖）"""
    if _backend() == "ollama":
        return os.getenv("LLM_MODEL", "qwen2.5:7b")
    return "deepseek-chat"


def _get_client():
    """返回 OpenAI 兼容客户端；无可用后端返回 None（走本地模板）。"""
    global _client, _OpenAI, _RETRYABLE
    backend = _backend()

    if backend == "offline":
        return None   # 强制模板（免费/离线）

    if backend == "ollama":
        # 本地免费生成：Ollama 的 OpenAI 兼容端点（无需 Key）
        if _client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise LLMError("已启用 LLM_BACKEND=ollama 但未安装 openai 库。请执行: pip install openai") from e
            _OpenAI = OpenAI
            _client = _OpenAI(
                api_key="ollama",
                base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "120")),
                max_retries=0,
            )
        return _client

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    if _client is None:
        try:
            from openai import OpenAI
            import openai as _oa
        except ImportError as e:
            raise LLMError(
                "已设置 DEEPSEEK_API_KEY 但未安装 openai 库。请执行: pip install openai"
            ) from e
        _OpenAI = OpenAI
        # openai SDK 各版本异常名兼容（3.x 起内部结构调整，个别类名可能迁移）
        _RETRYABLE = tuple(
            getattr(_oa, name) for name in ("RateLimitError", "APITimeoutError", "APIConnectionError")
            if hasattr(_oa, name)
        ) or (Exception,)
        _client = _OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "30")),
            max_retries=0,  # 重试由本模块统一控制（指数退避）
        )
    return _client


def complete(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> str:
    """统一 LLM 调用入口（带重试）。

    后端优先级：LLM_BACKEND=ollama（本地免费）> DEEPSEEK_API_KEY（云端）> 本地模板（免费）。
    模型默认值随后端变化；persona.generation.model 或界面 configure() 可覆盖。
    """
    client = _get_client()
    if client is None:
        return _local_fallback(prompt, system)

    # 模型解析：Ollama 后端 → 用 LLM_MODEL（忽略 persona 里的 deepseek-chat）；
    # 云端 → 界面 configure() > persona.generation.model > deepseek-chat
    if _backend() == "ollama":
        model = _OVERRIDES.get("model") or os.getenv("LLM_MODEL") or "qwen2.5:7b"
        max_tokens = min(_OVERRIDES.get("max_tokens") or max_tokens or 512, 800)
    else:
        model = _OVERRIDES.get("model") or model or _default_model()
        max_tokens = _OVERRIDES.get("max_tokens") or max_tokens or 1024
    temperature = _OVERRIDES.get("temperature", temperature)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            _record_usage(model, t0, resp)
            return resp.choices[0].message.content or ""
        except _RETRYABLE as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
        except Exception as e:  # noqa: BLE001 —— 非可重试错误（参数/鉴权等）直接失败
            raise LLMError(f"LLM 调用失败（非重试错误）: {e}") from e
    raise LLMError(f"LLM 调用失败（重试{max_retries}次后放弃）: {last_err}")


def _record_usage(model: str, t0: float, resp) -> None:
    """用量埋点：记录每次 LLM 调用的耗时 + token 数（失败静默）。"""
    try:
        from .usage import add_tokens, record
        latency_ms = round((time.time() - t0) * 1000.0, 1)
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        add_tokens(prompt_tokens, completion_tokens)
        record(
            "llm_call",
            backend=_backend(),
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    except Exception:  # noqa: BLE001 —— 埋点失败不影响生成
        pass


def _local_fallback(prompt: str, system: str) -> str:
    """离线降级：依据 prompt 关键词 + Persona 生成模板化内容。
    保证链路完整、风格接近，便于无 Key 演示与测试。"""
    # 判断优先级：正文 > 标题（避免“标题参考”等字样误判）
    has_body = "正文" in prompt
    has_title = "标题" in prompt and not has_body
    # 从 prompt 抽取主题
    topic = ""
    if "《" in prompt and "》" in prompt:
        topic = prompt.split("《")[1].split("》")[0]
    if not topic:
        topic = prompt.split("主题")[-1].strip().strip("。") if "主题" in prompt else "这个话题"

    if has_title:
        return f"1. {topic}｜谁懂啊，这样选绝了🍃\n2. 别再踩坑！{topic}干货合集💡\n3. {topic}亲测有效，冲鸭🍃"
    if has_body:
        return (
            f"家人们谁懂啊，今天必须聊聊{topic}。🍃\n\n"
            f"刚开始我也是一头雾水。后来试了一圈，才发现几个超实用的点。\n\n"
            f"别贪多，先把基础打牢。\n"
            f"多看真实案例，比干看理论强太多。\n"
            f"动手练才是王道，光收藏等于没学。💡\n\n"
            f"你们有什么好方法？评论区一起交流呀。冲鸭🍃"
        )
    return f"关于《{topic}》的占位内容"


def build_system_prompt(persona: dict) -> str:
    """由 Persona 配置生成 system prompt"""
    habits = "；".join(persona.get("habits", []))
    forbidden = "、".join(persona.get("forbidden", []))
    return (
        f"你是{persona.get('name', '小红书博主')}，语气：{persona.get('tone', 'warm_girly')}。"
        f"写作习惯：{habits}。"
        f"禁止使用：{forbidden}。"
        f"句式偏好：{persona.get('sentence_length', 'short')}。"
    )
