# SSE 客户端断连处理

## 问题与实现

原 HTTP 接口使用同步回答生成器。客户端关闭页面或网络中断时，接口没有显式传递取消信号，也没有明确关闭各层模型流。仅停止向客户端写 Token，不能证明上游连接和并发槽已经释放。

本次实现覆盖 API、助手编排和模型调用三层：

1. `api.py` 的 `_DisconnectAwareSSE` 在独立任务中每 50ms 调用 `await request.is_disconnected()`。即使仍在等待首 Token，也能检测断连；发现断连后取消本次响应任务组。
2. 响应类只保留一个 ASGI receive 消费者，不同时启动 Starlette 默认断连监听，兼容 ASGI 2.0 和 2.4 路径。发送时的 `OSError` 同样进入清理。
3. 接口改用 `AssistantOrchestrator.areply_stream()`。异步模型生成可取消；取消不会转换为业务失败降级，不向已经断开的客户端发送 `done` 或 `error`，也不作为模型错误重试。
4. 助手通过 `aclosing` 关闭模型生成器；`acomplete_stream()` 在 `finally` 关闭本次 SDK 响应流。使用 AnyIO shield 保护异步关闭操作，最多等待 5 秒，然后退出并发 Semaphore 的上下文。
5. 当取消发生在生成过程中，不把已输出片段保存为完整会话回答。记录 `assistant_stream_cancelled`，保留 `trace_id` 和 `thread_id`，不记录正文。
6. 同步 `reply_stream()` 保留给现有 UI；也补充显式关闭模型迭代器与同步 SDK 流，供调用方主动关闭生成器时释放连接。

没有改动路由、鉴权、SSE 的 token/done/error 事件格式和目录库的引用校验边界。目录库继续先校验完整答案再输出，不能为了实时流式体验提前泄露未经验证的主张。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sse_disconnect.py tests/test_api.py tests/test_catalog_assistant.py -q
```

断连测试直接驱动 ASGI receive/send，不依赖会缓冲完整响应的 TestClient 来模拟断连。测试覆盖：

- ASGI 2.0、2.4 下，在上游建立连接、等待首 Token、输出部分内容时断开。
- 服务任务取消、socket 发送失败、正常结束与可重试错误。
- 检查上游响应关闭、取消不重试、没有伪完成事件、生成中断不保存完整回答。
- 并发数为 1 时，取消后的下一次模型调用仍可完成，验证并发槽可复用。
- 真实 TCP 验证：本机启动 API 和 OpenAI 兼容的模拟模型服务，使用真实 OpenAI SDK；客户端收到首段后关闭连接，验证模拟模型服务收到上游关闭，且后续 API 请求可正常完成。
- 异步目录库回答仍然在输出前进行引用与原文支持检查。

上述本机模拟不需要真实 API Key，不访问收费模型。它证明请求取消与连接释放链路，不代表真实模型服务的算力或计费行为已经验证。

### 本次验证记录

2026-10-19，在当前工作区执行 `scripts/release_check.py`，通过 `PYTEST_ADDOPTS` 同时启用 CI 的 core/publishers 覆盖率门槛：

- 全量测试：239 passed、1 skipped；core/publishers 总覆盖率 75.79%，超过 55% 门槛。
- 本次新增 13 个流式/断连用例和 3 个异步目录库输出前校验用例。
- 内容库检查、密钥扫描、确定性安全红队、答案支持度、助手离线回归和 hashing RAG 门禁全部通过；总检查耗时 128.19 秒。
- 离线 CLI 生成运行成功；发布就绪门禁拦截未就绪稿件，没有执行真实发布。

结果与具体测试范围见 `SSE_DISCONNECT_VALIDATION.json`。这份覆盖率不包含 `api.py`，不能表述成 HTTP 接口覆盖率；ASGI 与真实 TCP 行为由专项测试单独验证。

## 边界

同步的检索、路由和 SQLite 校验仍通过线程池执行。已经运行的同步准备工作不能被 Python 强制安全中断；当前等待其返回，再响应取消，不继续启动该请求的回答生成。

关闭 SDK 响应流会终止本地继续读取并关闭该 HTTP 响应，不承诺远端服务立即停止推理或停止计费。共享模型客户端不会随单个请求关闭，以免影响其他请求。已经完成生成并进入最终保存的回答可能正常完成保存；这里保证的是生成过程中取消不会把半段答案当成完整回答。
