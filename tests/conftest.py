import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 测试强制离线：屏蔽 .env / 环境变量里的真实 Key 与本地 Ollama 后端，
# 保证 complete() 走本地降级，测试不依赖网络、账号余额或本机 Ollama。
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["LLM_BACKEND"] = "deepseek"
# RAG 回归也必须离线可复现；不能因为开发机 .env 选择了 bge-m3，
# 就在单元测试期间连接本机 Ollama。
os.environ["RAG_EMBEDDING_BACKEND"] = "hashing"
os.environ["RAG_QUERY_ENHANCER"] = "rule"
os.environ["RAG_ENABLE_HYDE"] = "0"
os.environ["RAG_RERANKER"] = "none"

# 触发 Skill 注册（@register 装饰器在 import 时执行）
import skills  # noqa: F401
