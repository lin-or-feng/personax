# 异步、并发与限流

更新：2026-10-22。当前支持 FastAPI 与版本化目录库；历史实验数据保留原模型和参数，不作为当前延迟承诺。

## 执行方式

| 路径 | 方式与作用域 |
|---|---|
| acomplete / acomplete_batch | 异步请求；批次共享当前事件循环的 Semaphore |
| acomplete_stream | 流生命周期占并发槽，结束或取消时关闭响应 |
| complete / complete_stream | 同步请求，受线程 Semaphore 约束 |
| HTTP JSON 的 Agent | AnyIO worker 回到父循环，复用异步配额 |
| HTTP SSE | 异步生成与取消；同步检索在线程中收尾 |
| Streamlit 的 Agent | 同步消费检查后分块的回答，内部使用短生命周期事件循环 |
| BM25、精确 kNN、重排 | 同步计算；asyncio 不使 CPU/GPU 算法本身变快 |
| 发布和依赖内容步骤 | 顺序执行，不并发操作同一发布页面 |

独立批次可以交错等待；存在数据依赖的检索、决策、回答与审核不能简单并行启动。异步不等于并行推理，也不保证单个请求更快。

## 两种限制

- Semaphore 限制同时在途的请求。Ollama 默认 1，云端默认 2；由 `LLM_MAX_CONCURRENCY` 配置。
- 滑动窗口限制最近 60 秒启动的请求数，由 `LLM_REQUESTS_PER_MINUTE` 配置。
- 先等 RPM 额度，再占 Semaphore，避免等待配额时占住在途槽。
- 每次真实尝试经过请求门；失败重试也消耗请求额度。
- API 实例级 RPM、Harness 的调用配额与 LLM 配额是不同边界。

异步 Semaphore 按事件循环共享，同步门另行管理；不存在跨所有入口、跨进程或分布式的统一并发上限。同一循环运行中不能改变配置上限，调整后需重启。

## 上下文与调用预算

短期历史同时限制最近消息数、估算 Token 和单条长度，超出时裁剪最旧消息。Checkpoint 可以保存更多消息，但不全部注入模型。

Agent 另外限制行动步骤、检索次数、估算 Token、单次决策与本轮期限。最大响应 Token 受调用方上限约束，不能被界面偏好放大。

同步检索在工作线程中完成后再关闭存储；取消不会强制终止该线程，因此同步 I/O 阻塞仍可能超出整轮墙钟期限。

## 客户端与取消

异步模型客户端按事件循环隔离。UI 每轮循环退出前关闭自己的连接池；HTTP 多请求共享父循环客户端。

SSE 取消只关闭本次模型响应，不关闭其他请求共用的客户端。取消不转为模型错误重试，未完成回答不写完整对话。清理策略及测试见 [SSE](SSE_DISCONNECT_HANDLING.md)。

## 历史测量

2026-09-21；对照只验证等待重叠和本机吞吐，不是生产负载测试。

| 后端与任务 | 串行 ms | 并发上限 | 异步 ms | 相对加速 |
|---|---:|---:|---:|---:|
| 模拟 I/O，8 × 250ms | 2004.1 | 2 | 1000.8 | 2.002× |
| Ollama 3B，4 个短请求 | 2277.0 | 2 | 2279.6 | 0.999× |
| Ollama 3B，4 个短请求 | 2277.0 | 4 | 2284.8 | 0.997× |

单卡模型在该次实验中没有吞吐提升，不能用模拟 I/O 的加速率宣传真实模型效果。3B 数据只保留为历史测量，不再用作当前助手模型。

原始数据：[模拟 I/O](ASYNC_BENCHMARK_SIMULATED.json)、[Ollama 并发 2](ASYNC_BENCHMARK_OLLAMA.json)、[Ollama 并发 4](ASYNC_BENCHMARK_OLLAMA_C4.json)。

```sh
python scripts/benchmark_async_llm.py --mode simulated --tasks 8 --concurrency 2 --delay-ms 250
```

真实后端压测需另行主动执行；同一 GPU 上增加并发可能只是排队、增加延迟和显存占用。
