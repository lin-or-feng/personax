"""联网检索增强（生成前抓热点/参考，让内容更有时效性）

后端（环境变量 WEB_SEARCH_BACKEND）：
- bing   默认：爬必应搜索结果（免费、免 Key）；网络差自动降级
- bocha  博查 AI 搜索（需 BOCHA_API_KEY，免费额度）
- tavily Tavily 搜索（需 TAVILY_API_KEY，免费额度）
- off    关闭

开关：WEB_SEARCH_ENABLED=1 开启；未开启或失败一律返回空，绝不影响生成主流程。

用法：
    from core.websearch import search_web, build_web_context
    ctx = build_web_context("秋招穿搭")     # → 格式化热点字符串（可能为 ""）
"""
from __future__ import annotations
import json
import os
import re
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_OVERRIDES: dict = {}   # 运行时覆盖（可视化界面开关/后端，优先于环境变量）


def configure(*, enabled: bool | None = None, backend: str | None = None):
    """运行时设置联网开关/后端（供可视化界面调用）"""
    if enabled is not None:
        _OVERRIDES["enabled"] = bool(enabled)
    if backend is not None:
        _OVERRIDES["backend"] = backend.strip().lower()


def _enabled() -> bool:
    if "enabled" in _OVERRIDES:
        return _OVERRIDES["enabled"]
    return os.getenv("WEB_SEARCH_ENABLED", "0") == "1"


def _backend() -> str:
    if "backend" in _OVERRIDES:
        return _OVERRIDES["backend"]
    return os.getenv("WEB_SEARCH_BACKEND", "bing").strip().lower()


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def _scrape_bing(query: str, top_k: int) -> list[str]:
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    items: list[str] = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.S):
        block = m.group(0)
        t = re.search(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
        s = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        title = _clean(t.group(1)) if t else ""
        snip = _clean(s.group(1)) if s else ""
        if title:
            items.append(f"{title}：{snip}" if snip else title)
    return items[:top_k]


def _search_bocha(query: str, top_k: int) -> list[str]:
    key = os.getenv("BOCHA_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        "https://api.bochaai.com/v1/web-search",
        data=json.dumps({"query": query, "count": top_k}).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    pages = (data.get("data") or {}).get("webPages", {}).get("value", []) or []
    out = []
    for i in pages:
        title = str(i.get("title") or "").strip()
        summary = str(i.get("summary") or "").strip()
        if title:
            out.append(f"{title}：{summary}" if summary else title)
    return out[:top_k]


def _search_tavily(query: str, top_k: int) -> list[str]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"api_key": key, "query": query, "max_results": top_k}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    return [f"{r.get('title','')}：{r.get('content','')}".strip("：")
            for r in data.get("results", [])][:top_k]


def search_web(query: str, top_k: int = 3) -> list[str]:
    """检索网页，返回 [标题：摘要, ...]。失败/未开启返回空列表。"""
    if not _enabled():
        return []
    backend = _backend()
    try:
        if backend == "off":
            return []
        if backend == "bocha":
            return _search_bocha(query, top_k)
        if backend == "tavily":
            return _search_tavily(query, top_k)
        return _scrape_bing(query, top_k)   # 默认
    except Exception:  # noqa: BLE001 —— 联网失败不阻断生成
        return []


_STOP_TOKENS = {"一个", "什么", "怎么", "如何", "这个", "那个", "攻略", "技巧",
                "方法", "推荐", "分享", "教程", "没有", "不是", "可以", "进来"}


def _topic_tokens(topic: str) -> set[str]:
    """取主题的有意义片段（整词 + 二元组，去虚词），用于过滤检索结果"""
    toks: set[str] = set()
    t = (topic or "").strip()
    if len(t) >= 2 and t not in _STOP_TOKENS:
        toks.add(t)
    for i in range(len(t) - 1):
        big = t[i:i + 2]
        if big not in _STOP_TOKENS:
            toks.add(big)
    return toks


def _relevant(results: list[str], topic: str) -> list[str]:
    """相关性门槛：结果必须包含主题的至少一个片段，避免跑题"""
    toks = _topic_tokens(topic)
    if not toks:
        return results
    return [r for r in results if any(tok in r for tok in toks)]


def build_web_context(topic: str, top_k: int = 3, max_len: int = 120) -> str:
    """按主题抓热点/参考，过滤相关性后格式化为提示词片段；失败/无关返回空串。"""
    if not _enabled():
        return ""
    results = search_web(f'"{topic}" 小红书 热门', top_k=top_k * 2)
    results = _relevant(results, topic)[:top_k]
    if not results:
        return ""
    joined = "\n".join(f"- {r[:max_len]}" for r in results)
    return ("\n\n以下是近期相关热点/信息（仅作时效性参考，请勿编造具体数据，"
            "可结合它们调整切入点）：\n" + joined)
