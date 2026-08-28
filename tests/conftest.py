import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 测试强制离线：屏蔽 .env / 环境变量里的真实 Key 与本地 Ollama 后端，
# 保证 complete() 走本地降级，测试不依赖网络、账号余额或本机 Ollama。
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["LLM_BACKEND"] = "deepseek"

# 触发 Skill 注册（@register 装饰器在 import 时执行）
import skills  # noqa: F401
