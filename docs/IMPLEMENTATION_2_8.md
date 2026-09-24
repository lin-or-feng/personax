# PersonaX 2.8 / 2.8.1：安全 JSON / SSE 服务接口与双语文档

## 1. 为什么这一阶段不继续堆 RAG 组件

2.7 已经具备混合检索、排序评测、回答支持度、真实反馈和本地可观测。下一步更缺的是一个能被前端、脚本和面试演示稳定调用的服务边界，而不是在小型知识库上过早引入 ANN、Redis 或多 Agent 并行。

2.8 因此只补一条最小服务链路：

```text
HTTP JSON / SSE
  -> Pydantic 边界校验 + 可选 Bearer Token
  -> AssistantOrchestrator
  -> 只读 StructuredTool / RAG / 单次回答 LLM
  -> AssistantResponse + trace_id
```

接口层没有导入 Publisher，也没有任何真实发布路由。

## 2. 接口契约

| 路径 | 用途 | 是否触发模型 |
|---|---|---|
| `GET /healthz` | 进程存活；不会初始化助手或模型 | 否 |
| `GET /readyz` | 校验配置并延迟创建共享助手服务 | 否 |
| `POST /v1/assistant/reply` | 返回完整 `AssistantResponse` JSON | 是 |
| `POST /v1/assistant/stream` | SSE：多个 `token` 事件，最后一个 `done` 事件 | 是 |

客户端不能提交 `trace_id`；服务端为每轮生成新 Trace，并同时写入 JSON/SSE 结果和 `X-Trace-ID` 响应头。未知字段、超过 20 条历史、超过 20 条记忆、超长文本和不安全 thread ID 在进入编排层前返回 422。

## 3. 并发与异步边界

- 当前 Ollama/OpenAI 兼容调用是同步实现，FastAPI 路由使用普通 `def`，由 ASGI 线程池承载，避免在事件循环中直接执行阻塞调用。
- SSE 使用普通生成器交给 `StreamingResponse`；生成器逐块产出，不先把完整回答缓存在内存后伪装流式。
- `AssistantOrchestrator` 在进程内共享，使 Harness 滑动窗口限流不会被每个 HTTP 请求重置。
- 首次 RAG 构建增加双重检查锁；并发首请求只创建一份索引。
- LLM 层原有 `Semaphore + RPM 滑动窗口` 继续约束真实模型并发和启动频率。
- Uvicorn 固定单 Worker。SQLite checkpoint、内存 Harness 与限流器不是跨进程共享状态；在换成共享数据库/限流后才能增加 Worker。

## 4. 安全边界

- 默认绑定 `127.0.0.1`；不启用 CORS，不默认暴露给浏览器跨域页面。
- 设置 `PERSONAX_API_TOKEN` 后，助手接口必须携带 `Authorization: Bearer ...`；比较使用常量时间函数。
- HTTP 入口使用实例级 60 RPM 滑动窗口，不能通过更换 `thread_id` 绕过；超限返回 429 与 `Retry-After`。
- 健康检查保持公开，但只返回版本、是否要求 Token 和“无发布接口”等低敏状态。
- API 异常返回服务端 Trace 与异常类型，不返回异常正文、提示词、Key 或检索内容。
- API 的 OpenAPI 路径集合不包含 publish；容器同时固定 `XHS_REAL_PUBLISH_ENABLED=0`。
- Docker API 仍使用非 root、只读根文件系统、`CapDrop=ALL`、`no-new-privileges`、PID 上限和本机端口绑定。

## 5. 运行

原生方式：

```powershell
cd /d D:\deepseekworkspace\personax_project
.\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --workers 1
```

命令保持在当前终端前台运行并显示完整日志；浏览器手动打开 `http://127.0.0.1:8000/docs`。项目不新增隐藏启动、自启动、注册表项、计划任务或开机常驻脚本。

2.8.1 的文档入口：

| 地址 | 说明 |
|---|---|
| `/`、`/docs` | 307 跳转到默认简中页 |
| `/docs/zh-CN` | 简体中文操作说明，技术字段保持英文 |
| `/docs/en` | English reference |
| `/openapi/zh-CN.json` | 简中说明的 OpenAPI 3.1 JSON |
| `/openapi/en.json` | English OpenAPI 3.1 JSON |

简中模式只本地化接口用途、字段说明和常见校验错误；URL、HTTP 方法、JSON 字段、Bearer、SSE 事件名等技术契约不翻译。页面顶部可切换语言，Swagger UI 固有操作按钮继续使用英文。

Docker 方式：

```powershell
docker compose --profile api up -d --build api --wait
```

默认 API 地址为 `http://127.0.0.1:8000`。如需令牌：

```powershell
$env:PERSONAX_API_TOKEN = "由密码管理器生成的随机长字符串"
docker compose --profile api up -d api --wait
```

## 6. 验收结果与未声称项

- FastAPI 边界专项与相关回归：新增默认跳转、双语页面、双语 OpenAPI 与未知语言回归；API 专项 12 passed。
- 完整单元测试：159 passed。
- 原生 Uvicorn：health、ready、JSON、SSE 均 HTTP 200，SSE 包含 `token` 与 `done`。
- Docker：最终代码状态的 `personax:2.8.0` 实机构建成功，API 容器 healthy；JSON/SSE 冒烟通过，配置为 2 RPM 时第三次请求返回 429 和 `Retry-After: 60`。
- 容器实测 UID 100，`Restart=no`、`ReadonlyRootfs=true`、PID 256、`CapDrop=[ALL]`、`no-new-privileges=true`。
- 验收使用 `LLM_BACKEND=offline`，没有调用云端模型、没有登录平台、没有真实发布。
- 验收结束后测试容器已停止并删除，Docker Desktop 已通过官方 CLI 停止；只保留可复用镜像和命名卷。

Docker 实机构建证据来自 2.8.0 服务边界；2.8.1 仅改动文档路由、OpenAPI 元数据与请求示例，本轮未把“单测通过”等同于新的跨机镜像验收。

这一版不声称支持公网生产部署、多 Worker、分布式限流、Redis 会话、断线续传或线上 SLA。SSE 只保证单连接的有序 token/done 事件；断线后由客户端以新请求重试。
