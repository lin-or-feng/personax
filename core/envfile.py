"""极简 .env 加载器（零依赖，不引入 python-dotenv）

在项目根目录放 .env 文件即可配置密钥，格式：
    DEEPSEEK_API_KEY=sk-xxxx
    DEEPSEEK_BASE_URL=https://api.deepseek.com   # 可选
    DEEPSEEK_TIMEOUT=30                           # 可选

规则：只做 setdefault（系统环境变量优先）；忽略 # 注释与空行；
引号会被去除。参考 .env.example。
"""
from __future__ import annotations
import os
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    loaded: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return loaded
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            loaded[key] = value
            os.environ.setdefault(key, value)   # 已有环境变量优先，不覆盖
    return loaded
