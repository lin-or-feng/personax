# PersonaX 验证记录

> 最后更新：2026-09-16（Asia/Shanghai）  
> 验证边界：本地静态扫描、单元测试和离线评测；未登录或调用小红书，未触发真实发布。

## 阶段 1：安全基线

| 检查 | 命令 | 结果 |
|---|---|---|
| 密钥与敏感文件 | `python scripts/check_secrets.py --strict` | 通过；扫描 198 个文件，0 个可提交危险项 |
| 安全专项回归 | `python -m pytest tests/test_security.py` | 6 passed，0 failed |
| 完整单测 | `python -m pytest tests` | 79 passed，0 failed，0.92s |
| Python 语法检查 | `python -m py_compile app.py core/assistant.py core/assistant_tools.py` | 通过 |
| Git 差异格式 | `git diff --check` | 通过 |

安全专项覆盖：

- 来源 URL 仅接受 `http/https`，拒绝 `javascript/file/data` 与带内嵌凭据的链接。
- 知识库片段作为不可信 JSON 资料注入，无法闭合提示词边界。
- Streamlit 会话重跑时复用 Harness，限流计数不被重置。
- SQLite checkpoint 使用参数化查询，恶意 `thread_id` 不会改变表结构；损坏 JSON 安全回退为空历史。
- AI 助手工具清单仅有只读知识检索，不包含发布工具。

## 剩余风险与不声称项

- 静态扫描能降低误提交凭据的风险，不等于专业 SAST/DAST 或依赖供应链审计。
- 本轮未用受限账号做平台端到端发布测试，这是刻意的安全边界，不代表发布流程已在当前平台状态下验收。
- 本地 `.env`、登录态和发布日志存在但已被 `.gitignore` 忽略；它们不在本次提交范围。
