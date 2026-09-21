"""LLM 客户端：DeepSeek（OpenAI 兼容协议）——商用健壮版

- 环境变量 DEEPSEEK_API_KEY 必填才走真实模型；未设置时返回占位文本（离线演示/测试）
- openai 库惰性导入：未安装时若设置了 Key，抛出带安装提示的 LLMError
- 真实调用：超时 + 指数退避重试（默认 3 次），仅对可重试错误（限流/超时/断连）重试
- 统一入口 core.llm.complete()，业务 Skill 禁止直接 new client
"""
from __future__ import annotations
import asyncio
import os
import threading
import time
import weakref
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Optional

from .async_runtime import AsyncBatchResult, gather_bounded
from .limits import SlidingWindowRateLimiter


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
_CLIENT_BACKEND: str | None = None
_async_client: Optional[object] = None
_ASYNC_CLIENT_BACKEND: str | None = None
_OpenAI = None
_RETRYABLE: tuple = ()
_OVERRIDES: dict = {}   # 运行时覆盖（可视化界面设置后端/模型/温度）
_OLLAMA_CHECK: dict = {"ts": 0.0, "ok": False}   # 本机 Ollama 可达性缓存（5s）
_ASYNC_SEMAPHORES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SYNC_SEMAPHORES: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_REQUEST_LIMITERS: dict[tuple[str, int], SlidingWindowRateLimiter] = {}
_LIMIT_STATE_LOCK = threading.Lock()


def configure(*, backend: str | None = None, model: str | None = None,
              temperature: float | None = None, max_tokens: int | None = None):
    """运行时覆盖生成参数（供可视化界面调用；不设置则用默认值）。

    backend: "ollama"（本地免费）/ "deepseek"（云端）/ "offline"（模板）
    """
    global _client, _CLIENT_BACKEND, _async_client, _ASYNC_CLIENT_BACKEND
    if backend is not None:
        normalized_backend = backend.strip().lower()
        if normalized_backend != _OVERRIDES.get("backend"):
            # OpenAI-compatible clients carry a fixed base_url. Switching from
            # Ollama to DeepSeek (or back) must not reuse the previous client.
            _client = None
            _CLIENT_BACKEND = None
            _async_client = None
            _ASYNC_CLIENT_BACKEND = None
        _OVERRIDES["backend"] = normalized_backend
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
    global _client, _CLIENT_BACKEND, _OpenAI, _RETRYABLE
    backend = _backend()

    if _CLIENT_BACKEND is not None and _CLIENT_BACKEND != backend:
        _client = None
        _CLIENT_BACKEND = None

    if backend == "offline":
        return None   # 强制模板（免费/离线）

    if backend == "ollama":
        # 本地免费生成：Ollama 的 OpenAI 兼容端点（无需 Key）
        if _client is None:
            try:
                from openai import OpenAI
                import openai as _oa
            except ImportError as e:
                raise LLMError("已启用 LLM_BACKEND=ollama 但未安装 openai 库。请执行: pip install openai") from e
            _OpenAI = OpenAI
            _RETRYABLE = tuple(
                getattr(_oa, name)
                for name in ("RateLimitError", "APITimeoutError", "APIConnectionError")
                if hasattr(_oa, name)
            ) or (Exception,)
            _client = _OpenAI(
                api_key="ollama",
                base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "120")),
                max_retries=0,
            )
            _CLIENT_BACKEND = backend
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
        _CLIENT_BACKEND = backend
    return _client


def _get_async_client():
    """返回与当前后端匹配的 AsyncOpenAI 客户端。"""

    global _async_client, _ASYNC_CLIENT_BACKEND, _RETRYABLE
    backend = _backend()
    if _ASYNC_CLIENT_BACKEND is not None and _ASYNC_CLIENT_BACKEND != backend:
        _async_client = None
        _ASYNC_CLIENT_BACKEND = None

    if backend == "offline":
        return None

    try:
        from openai import AsyncOpenAI
        import openai as _oa
    except ImportError as exc:
        raise LLMError("异步 LLM 需要 openai 库，请执行: pip install openai") from exc

    _RETRYABLE = tuple(
        getattr(_oa, name)
        for name in ("RateLimitError", "APITimeoutError", "APIConnectionError")
        if hasattr(_oa, name)
    ) or (Exception,)

    if _async_client is None:
        if backend == "ollama":
            api_key = "ollama"
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "120"))
        else:
            api_key = os.getenv("DEEPSEEK_API_KEY")
            if not api_key:
                return None
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
        _async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        _ASYNC_CLIENT_BACKEND = backend
    return _async_client


def _async_limit(value: int | None = None) -> int:
    default_limit = "1" if _backend() == "ollama" else "2"
    raw = value if value is not None else os.getenv("LLM_MAX_CONCURRENCY", default_limit)
    try:
        return max(1, min(int(raw), 16))
    except (TypeError, ValueError):
        return 2


def _requests_per_minute() -> int:
    """逻辑请求启动频率；与同时在途数是两个独立维度。"""

    try:
        return max(1, min(int(os.getenv("LLM_REQUESTS_PER_MINUTE", "60")), 600))
    except (TypeError, ValueError):
        return 60


def _request_limiter() -> SlidingWindowRateLimiter:
    backend = _backend()
    rpm = _requests_per_minute()
    key = (backend, rpm)
    with _LIMIT_STATE_LOCK:
        limiter = _REQUEST_LIMITERS.get(key)
        if limiter is None:
            limiter = SlidingWindowRateLimiter(rpm, 60.0)
            _REQUEST_LIMITERS[key] = limiter
        return limiter


def _sync_semaphore() -> threading.BoundedSemaphore:
    """同步 Streamlit/CLI 调用也要受并发门保护。"""

    backend = _backend()
    normalized_limit = _async_limit()
    with _LIMIT_STATE_LOCK:
        state = _SYNC_SEMAPHORES.get(backend)
        if state is None:
            semaphore = threading.BoundedSemaphore(normalized_limit)
            _SYNC_SEMAPHORES[backend] = (normalized_limit, semaphore)
            return semaphore
        configured_limit, semaphore = state
        if configured_limit != normalized_limit:
            raise LLMError(
                "运行中不能动态切换同步 LLM 并发上限；"
                f"当前为 {configured_limit}，请求为 {normalized_limit}。"
                "请修改 LLM_MAX_CONCURRENCY 后重启服务。"
            )
        return semaphore


def _loop_semaphore(limit: int | None = None) -> asyncio.Semaphore:
    """每个事件循环共享一个 Semaphore，避免批次之间绕过 API 限流。"""

    event_loop = asyncio.get_running_loop()
    normalized_limit = _async_limit(limit)
    state = _ASYNC_SEMAPHORES.get(event_loop)
    if state is None:
        semaphore = asyncio.Semaphore(normalized_limit)
        _ASYNC_SEMAPHORES[event_loop] = (normalized_limit, semaphore)
        return semaphore
    configured_limit, semaphore = state
    if configured_limit != normalized_limit:
        raise LLMError(
            "同一事件循环内不能动态切换 LLM 并发上限；"
            f"当前为 {configured_limit}，请求为 {normalized_limit}。"
            "请统一 LLM_MAX_CONCURRENCY 并重启服务。"
        )
    return semaphore


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
            # 先等滑动窗口额度，再占并发槽；避免等待 RPM 时空占 Semaphore。
            _request_limiter().acquire(_backend())
            t0 = time.time()
            with _sync_semaphore():
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


def complete_stream(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Iterator[str]:
    """统一的真实流式 LLM 入口。

    仅在尚未向调用方输出任何文本时重试，避免断流后重试造成重复回答。
    离线模板也按小块返回，方便 UI 与真实后端共用同一条渲染链路。
    """

    client = _get_client()
    if client is None:
        fallback = _local_fallback(prompt, system)
        for offset in range(0, len(fallback), 12):
            yield fallback[offset:offset + 12]
        return

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
        emitted = False
        last_chunk = None
        try:
            _request_limiter().acquire(_backend())
            t0 = time.time()
            with _sync_semaphore():
                response_stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for chunk in response_stream:
                    last_chunk = chunk
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None)
                    if content:
                        emitted = True
                        yield str(content)
            _record_usage(model, t0, last_chunk)
            return
        except _RETRYABLE as exc:
            last_err = exc
            if emitted:
                raise LLMError(f"LLM 流式输出中断，已保留已生成内容: {exc}") from exc
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM 流式调用失败（非重试错误）: {exc}") from exc
    raise LLMError(f"LLM 流式调用失败（重试{max_retries}次后放弃）: {last_err}")


def _async_request_values(
    prompt: str,
    system: str,
    model: str | None,
    temperature: float,
    max_tokens: int | None,
) -> tuple[str, float, int, list[dict[str, str]]]:
    if _backend() == "ollama":
        resolved_model = _OVERRIDES.get("model") or os.getenv("LLM_MODEL") or "qwen2.5:7b"
        resolved_tokens = min(_OVERRIDES.get("max_tokens") or max_tokens or 512, 800)
    else:
        resolved_model = _OVERRIDES.get("model") or model or _default_model()
        resolved_tokens = _OVERRIDES.get("max_tokens") or max_tokens or 1024
    resolved_temperature = float(_OVERRIDES.get("temperature", temperature))
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return resolved_model, resolved_temperature, int(resolved_tokens), messages


async def _acomplete_unbounded(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_concurrency: int | None = None,
) -> str:
    """异步重试核心；每次真实尝试都经过 RPM + Semaphore 双门。"""

    client = _get_async_client()
    if client is None:
        await asyncio.sleep(0)
        return _local_fallback(prompt, system)
    resolved_model, resolved_temperature, resolved_tokens, messages = _async_request_values(
        prompt, system, model, temperature, max_tokens)
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            await _request_limiter().acquire_async(_backend())
            t0 = time.time()
            async with _loop_semaphore(max_concurrency):
                response = await client.chat.completions.create(
                    model=resolved_model,
                    messages=messages,
                    temperature=resolved_temperature,
                    max_tokens=resolved_tokens,
                )
            _record_usage(resolved_model, t0, response)
            return response.choices[0].message.content or ""
        except _RETRYABLE as exc:
            last_err = exc
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"异步 LLM 调用失败（非重试错误）: {exc}") from exc
    raise LLMError(f"异步 LLM 调用失败（重试{max_retries}次后放弃）: {last_err}")


async def acomplete(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_concurrency: int | None = None,
) -> str:
    """带全局有界并发门的异步 LLM 调用。"""

    return await _acomplete_unbounded(
        prompt,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        max_retries=max_retries,
        base_delay=base_delay,
        max_concurrency=max_concurrency,
    )


async def acomplete_batch(
    prompts: Sequence[str],
    *,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_concurrency: int | None = None,
    return_exceptions: bool = False,
) -> AsyncBatchResult[str]:
    """批量异步调用；Semaphore 只限制真实在途 API 请求。"""

    async def worker(prompt: str) -> str:
        # 每个批次先由 gather_bounded 控制任务创建速度，再由 acomplete 的
        # 事件循环级 Semaphore 控制所有批次合计的真实在途 API 请求。
        return await acomplete(
            prompt,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
            base_delay=base_delay,
            max_concurrency=max_concurrency,
        )

    return await gather_bounded(
        list(prompts),
        worker,
        max_concurrency=_async_limit(max_concurrency),
        return_exceptions=return_exceptions,
    )


async def acomplete_stream(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_concurrency: int | None = None,
) -> AsyncIterator[str]:
    """异步流式入口；整个流生命周期都占用一个 Semaphore 槽位。"""

    client = _get_async_client()
    if client is None:
        fallback = _local_fallback(prompt, system)
        for offset in range(0, len(fallback), 12):
            await asyncio.sleep(0)
            yield fallback[offset:offset + 12]
        return

    resolved_model, resolved_temperature, resolved_tokens, messages = _async_request_values(
        prompt, system, model, temperature, max_tokens)
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        emitted = False
        last_chunk = None
        try:
            await _request_limiter().acquire_async(_backend())
            t0 = time.time()
            async with _loop_semaphore(max_concurrency):
                response_stream = await client.chat.completions.create(
                    model=resolved_model,
                    messages=messages,
                    temperature=resolved_temperature,
                    max_tokens=resolved_tokens,
                    stream=True,
                )
                async for chunk in response_stream:
                    last_chunk = chunk
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None)
                    if content:
                        emitted = True
                        yield str(content)
            _record_usage(resolved_model, t0, last_chunk)
            return
        except _RETRYABLE as exc:
            last_err = exc
            if emitted:
                raise LLMError(f"异步流式输出中断，已保留已生成内容: {exc}") from exc
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"异步流式调用失败（非重试错误）: {exc}") from exc
    raise LLMError(f"异步流式调用失败（重试{max_retries}次后放弃）: {last_err}")


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
