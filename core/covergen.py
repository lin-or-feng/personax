"""封面图生成（多模态 · 增强版）

本地海报（默认，零成本）4 种风格，按主题哈希自动选取（同一主题风格一致）：
- gradient  渐变大字（默认）
- split     撞色几何
- minimal   极简白底
- card      复古卡片

AI 背景增强（可选，配 Key 自动启用）：COVER_AI_ENABLED=1 + SILICONFLOW_API_KEY
  → 用文生图生成背景图 + PIL 叠加标题文字（文字清晰、背景精美）。
  无 Key/失败自动回退本地海报，绝不影响发布。

运行时控制：covergen.configure(style=..., ai_enabled=...)（可视化界面调用）

用法：
    from core.covergen import generate_cover, ensure_cover_for_draft
"""
from __future__ import annotations
import os
import random
import time
from pathlib import Path

# 配色盘（本地海报）
PALETTES = [
    ((255, 183, 197), (255, 129, 155)),   # 樱花粉
    ((164, 205, 255), (102, 153, 255)),   # 天空蓝
    ((255, 214, 165), (255, 156, 90)),    # 蜜橙
    ((200, 214, 255), (138, 130, 255)),   # 雾紫
    ((180, 240, 205), (84, 195, 132)),    # 薄荷绿
    ((255, 225, 165), (255, 187, 90)),    # 麦黄
    ((210, 205, 255), (150, 120, 255)),   # 薰衣草
    ((255, 205, 210), (255, 120, 140)),   # 珊瑚
]

# 莫兰迪暗调配色（premium 高级感）
MUTED_PALETTES = [
    ((31, 41, 59), (55, 65, 81), (201, 168, 106)),    # 深灰蓝 + 金
    ((30, 58, 47), (46, 94, 76), (214, 186, 132)),    # 墨绿 + 米金
    ((55, 40, 34), (92, 66, 52), (205, 172, 138)),    # 深咖 + 暖金
    ((60, 30, 40), (96, 48, 60), (216, 176, 186)),    # 酒红 + 玫瑰金
    ((35, 46, 62), (58, 74, 96), (150, 178, 205)),    # 雾蓝 + 银蓝
]

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_OVERRIDES: dict = {}


def configure(*, style: str | None = None, ai_enabled: bool | None = None):
    """运行时覆盖封面风格/AI 背景开关"""
    if style is not None:
        _OVERRIDES["style"] = style
    if ai_enabled is not None:
        _OVERRIDES["ai_enabled"] = bool(ai_enabled)


def _style() -> str:
    s = _OVERRIDES.get("style") or os.getenv("COVER_STYLE", "")
    return s if s in ("premium", "minimal", "gradient", "split", "card") else "premium"


def _ai_enabled() -> bool:
    if "ai_enabled" in _OVERRIDES:
        return _OVERRIDES["ai_enabled"]
    return os.getenv("COVER_AI_ENABLED", "0") == "1"


def _font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _palette_for(topic: str, title: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    h = hash((topic or "") + (title or "")) % len(PALETTES)
    return PALETTES[h]


def _wrap_title(title: str, max_lines: int = 4) -> list[str]:
    title = (title or "").strip() or "笔记分享"
    lines: list[str] = []
    cur = ""
    for ch in title:
        if len(cur) >= 12:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:11] + "…"
    return lines


def _draw_title(draw, lines, font, w, cy, color=(255, 255, 255), shadow=(0, 0, 0)):
    y = cy - (len(lines) - 1) * 34
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (w - tw) // 2
        draw.text((x + 3, y + 3), line, font=font, fill=shadow)
        draw.text((x, y), line, font=font, fill=color)
        y += th + 20


def _draw_topic_tag(draw, topic, w, h, color, chip: bool = True):
    tag = f"# {topic}"
    tf = _font(26)
    bbox = draw.textbbox((0, 0), tag, font=tf)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if chip:
        pad = 16
        box = [w // 2 - tw // 2 - pad, h - 140, w // 2 + tw // 2 + pad, h - 140 + th + pad * 2]
        draw.rounded_rectangle(box, radius=24, fill=(255, 255, 255))
        draw.text((w // 2 - tw // 2, h - 140 + pad), tag, font=tf, fill=color)
    else:
        draw.text((w // 2 - tw // 2, h - 120), tag, font=tf, fill=color)


def _vertical_gradient(w, h, top, bottom):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=c)
    return img, draw


# ---------- 5 种封面风格（premium/minimal 为高级感主推） ----------

def _muted_for(topic: str, title: str):
    h = hash((topic or "") + (title or "")) % len(MUTED_PALETTES)
    return MUTED_PALETTES[h]


def _style_premium(draw, w, h, top, bottom, accent):
    """高级暗调：深色莫兰迪底 + 金色细横线 + 极简圆环"""
    draw.line([(w // 2 - 45, int(h * 0.55)), (w // 2 + 45, int(h * 0.55))],
              fill=accent, width=2)
    draw.ellipse([w - 90, 50, w - 50, 90], outline=accent, width=2)
    draw.ellipse([30, h - 110, 66, h - 74], fill=accent)


def _style_minimal_clean(draw, w, h, top, bottom, accent):
    """留白极简：米白底 + 大黑字 + 细灰线 + 小色块"""
    draw.rectangle([28, 28, 34, 34], fill=accent)
    draw.line([(w // 2 - 40, int(h * 0.55)), (w // 2 + 40, int(h * 0.55))],
              fill=(205, 208, 214), width=2)


def _style_gradient(draw, w, h, top, bottom):
    draw.ellipse([w - 130, -60, w + 60, 130], outline=(255, 255, 255), width=14)
    draw.ellipse([-70, h - 150, 80, h + 50], outline=(255, 255, 255), width=10)


def _style_split(draw, w, h, top, bottom):
    from PIL import ImageDraw
    draw.polygon([(0, 0), (w, 0), (0, h)], fill=top)
    draw.ellipse([w - 200, h - 200, w + 40, h + 40], fill=bottom)
    draw.ellipse([-120, -120, 120, 120], fill=(255, 255, 255))


def _style_minimal(draw, w, h, top, bottom):
    draw.rectangle([0, 0, w, 22], fill=top)
    draw.rectangle([0, h - 22, w, h], fill=top)
    draw.ellipse([w - 160, 60, w - 40, 180], outline=top, width=8)


def _style_card(draw, w, h, top, bottom):
    draw.rectangle([24, 24, w - 24, h - 24], outline=top, width=6)
    draw.rectangle([40, 40, w - 40, h - 40], outline=top, width=2)
    draw.ellipse([-80, h - 180, 120, h + 20], fill=bottom)


_STYLE_FN = {"premium": _style_premium, "minimal": _style_minimal_clean,
             "gradient": _style_gradient, "split": _style_split,
             "minimal_old": _style_minimal, "card": _style_card}
# 标题文字颜色
_STYLE_TEXT = {"premium": (244, 245, 248), "minimal": (34, 36, 40),
               "gradient": (255, 255, 255), "split": (255, 255, 255),
               "card": (60, 60, 70)}
# 标签是否用白色圆角卡
_STYLE_CHIP = {"premium": False, "minimal": False,
               "gradient": True, "split": True, "card": True}
# 标题字号
_STYLE_FONT = {"premium": 50, "minimal": 62, "gradient": 58, "split": 58, "card": 58}


# ---------- AI 背景（可选） ----------

def _ai_background(prompt: str, size: tuple[int, int]) -> "Image.Image | None":
    """SiliconFlow 文生图生成背景（OpenAI 风格同步接口），失败返回 None"""
    key = os.getenv("SILICONFLOW_API_KEY")
    if not key:
        return None
    import base64
    import json
    import urllib.request
    body = json.dumps({
        "model": os.getenv("COVER_AI_MODEL", "black-forest-labs/FLUX.1-schnell"),
        "prompt": prompt, "image_size": f"{size[0]}x{size[1]}",
        "batch_size": 1, "response_format": "b64_json",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/images/generations",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    b64 = (data.get("data") or [{}])[0].get("b64_json")
    if not b64:
        return None
    from PIL import Image
    import io
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _ai_prompt(title: str, topic: str) -> str:
    return (f"小红书笔记封面背景图，主题：{topic}，标题：{title}。"
            "柔和渐变背景，简约高级感，留出中央空白区域放标题文字，"
            "无文字，无logo，竖版3:4，电商插画风格")


# ---------- 描述驱动生成 ----------

_COLOR_KEYWORDS = {
    "粉": ((255, 183, 197), (255, 129, 155), (255, 255, 255)),
    "蓝": ((164, 205, 255), (102, 153, 255), (255, 255, 255)),
    "绿": ((180, 240, 205), (84, 195, 132), (255, 255, 255)),
    "紫": ((200, 214, 255), (138, 130, 255), (255, 255, 255)),
    "橙": ((255, 214, 165), (255, 156, 90), (255, 255, 255)),
    "黄": ((255, 225, 165), (255, 187, 90), (60, 60, 40)),
    "红": ((255, 205, 210), (255, 120, 140), (255, 255, 255)),
    "黑": ((40, 44, 52), (70, 76, 88), (244, 245, 248)),
    "深": ((40, 44, 52), (70, 76, 88), (244, 245, 248)),
    "金": ((64, 52, 34), (120, 96, 58), (216, 186, 132)),
}

_STYLE_DESC_KEYWORDS = [
    ("premium", ("高级", "暗", "深色", "质感", "金", "商务")),
    ("minimal", ("简约", "极简", "干净", "留白", "白", "轻")),
    ("split", ("撞色", "几何", "大胆")),
    ("card", ("复古", "文艺", "卡", "边框")),
    ("gradient", ("渐变", "可爱", "清新", "少女", "温柔")),
]


def _parse_style_desc(desc: str) -> dict:
    """从用户描述中解析封面参数（style + 配色 + 强调色），解析不到则保持默认"""
    out: dict = {}
    d = desc or ""
    # 风格
    for style, kws in _STYLE_DESC_KEYWORDS:
        if any(k in d for k in kws):
            out["style"] = style
            break
    # 配色
    for kw, (a, b, accent) in _COLOR_KEYWORDS.items():
        if kw in d:
            out["palette"] = (a, b)
            out["accent"] = accent
            break
    return out


# ---------- 主入口 ----------

def generate_cover(
    title: str,
    topic: str = "",
    out_dir: str | Path = "assets/covers",
    size: tuple[int, int] = (600, 800),
    description: str = "",
) -> Path:
    """生成封面（AI 背景或本地海报），返回 PNG 路径。

    description：用户描述封面（如「粉色渐变 可爱风」）→
      AI 后端：直接作为图生图提示词；本地海报：解析风格/配色关键词。
    """
    from PIL import Image, ImageDraw

    w, h = size
    style = _style()

    # 描述解析
    parsed = _parse_style_desc(description) if description else {}
    if parsed.get("style"):
        style = parsed["style"]
    palette = parsed.get("palette")
    accent = parsed.get("accent")

    # 配色：premium 用莫兰迪暗调（带金色点缀）；minimal 用米白；其余用明亮色板
    if palette:
        top, bottom = palette
    elif style == "premium":
        (a, b, accent) = _muted_for(topic, title)
        top, bottom = a, b
    elif style == "minimal":
        top, bottom = (250, 250, 252), (244, 244, 248)
        accent = accent or _muted_for(topic, title)[2]
    else:
        top, bottom = _palette_for(topic, title)

    # AI 背景优先（有描述时直接用描述作图生图提示词）
    img = None
    if _ai_enabled():
        try:
            prompt = description if description else _ai_prompt(title, topic)
            img = _ai_background(prompt, size)
            if img:
                img = img.resize((w, h))
                print(f"[cover] 使用 AI 背景（{os.getenv('COVER_AI_MODEL', 'FLUX.1-schnell')}）")
        except Exception as e:  # noqa: BLE001
            print(f"[cover] AI 背景失败，回退本地海报: {e}")

    if img is None:
        img, draw = _vertical_gradient(w, h, top, bottom)
        fn = _STYLE_FN[style]
        if style in ("premium", "minimal"):
            fn(draw, w, h, top, bottom, accent)
        else:
            fn(draw, w, h, top, bottom)
    else:
        draw = ImageDraw.Draw(img)

    # 标题（AI 背景上同样叠加，保证文字清晰）
    text_color = _STYLE_TEXT[style]
    font = _font(_STYLE_FONT[style])
    lines = _wrap_title(title)
    _draw_title(draw, lines, font, w, int(h * 0.34), color=text_color,
                shadow=(0, 0, 0) if style != "minimal" else (255, 255, 255))
    if topic:
        if style == "premium":
            tag_color = accent or top
        elif style == "minimal":
            tag_color = (140, 143, 150)
        else:
            tag_color = top
        _draw_topic_tag(draw, topic, w, h, tag_color, chip=_STYLE_CHIP[style])

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"cover_{int(time.time())}_{random.randint(100, 999)}.png"
    img.save(path)
    return path


def ensure_cover_for_draft(draft, out_dir: str | Path = "assets/covers") -> str | None:
    """给草稿生成封面：写 metadata.cover 并前置到 images（无标题则不动）"""
    title = (getattr(draft, "title", None) or "").strip()
    if not title:
        return None
    topic = getattr(draft, "topic", "") or ""
    desc = (getattr(draft, "metadata", None) or {}).get("cover_desc") or ""
    path = generate_cover(title, topic, out_dir, description=desc)
    md = dict(getattr(draft, "metadata", None) or {})
    md["cover"] = str(path)
    images = list(md.get("images", []) or [])
    if str(path) not in images:
        images.insert(0, str(path))
    md["images"] = images
    draft.metadata = md
    return str(path)
