# PersonaX 异步、并发与限流设计

> 实测日期：2026-09-21；设备：本机 Ollama `qwen2.5:3b`。结果用于工程决策，不代表线上 SLA。

## 先区分三个概念

| 概念 | PersonaX 中的含义 | 是否一定更快 |
|---|---|---|
| 异步 async | 请求等待网络或模型响应时，把事件循环让给其他任务 | 单个请求通常不会更快 |
| 并发 concurrency | 多个任务在同一时间段交错推进；本项目用 async task + Semaphore | I/O 服务允许重叠时吞吐会提高 |
| 并行 parallelism | CPU/GPU 同时真正执行多个计算 | 不由 `asyncio` 保证，取决于模型服务和硬件 |

`stream=True` 也不等于 async：当前 Streamlit 单轮聊天是**同步流式**，优点是首字更快出现；新增的 `acomplete_stream()` 才是异步流式入口，适合未来 ASGI/FastAPI 服务。

## 当前项目哪些是并发、哪些是异步

| 模块 | 当前执行方式 | 原因与边界 |
|---|---|---|
| `core.llm.acomplete()` | 异步 + 滑动窗口 + Semaphore | 每次真实请求先获取 RPM 额度，再获取在途槽位；本地默认 1，云端默认 2 |
| `core.llm.acomplete_batch()` | 异步并发 | 多个独立提示词并发，结果仍按输入顺序返回，并记录峰值并发/失败数 |
| `core.llm.acomplete_stream()` | 异步流式 + Semaphore | 整个流生命周期占一个槽位，避免流式连接绕过限流 |
| `core.llm.complete_stream()` + Streamlit | 同步流式 + 滑动窗口 + 线程 Semaphore | 单用户聊天强调首字体验；同步不等于无限并发，改成 async 也不会减少模型推理时间 |
| Supervisor → Retriever → Answerer → Reviewer | 串行依赖 | 后一步需要前一步结果，不能并发 |
| LangGraph 内容节点 | 串行依赖 | 标题、正文、风格检查、发布门禁存在数据依赖 |
| BM25 + dense + RRF | 同步检索；embedding 批处理 | 向量批处理比对同一模型发大量并发请求更适合单卡 |
| Cross-encoder 重排 | 同步 CPU/GPU 计算 | 属于计算密集，不应靠 asyncio 提速 |
| Playwright 登录/发布 | 串行副作用 | 页面操作顺序固定，且并发发布会增加账号风控风险 |
| 多 Agent Worker | 逻辑职责拆分，不是并行进程 | Retriever/Answerer/Reviewer 默认依次执行 |

## 基本流程

### 单轮聊天

```text
用户输入
  → Supervisor 路由
  → 可选 RAG 检索
  → LLM 流式回答
  → Reviewer 引用/证据检查
  → 完成后写入 SQLite Checkpoint
```

这条链路以依赖关系为主。正确优化是流式显示、缓存、批量 embedding 和减少无效调用，而不是把四个节点同时启动。

### 独立批量请求

```text
N 个独立 prompt
  → 创建 N 个 async task
  → 滑动窗口等待请求启动额度
  → asyncio.Semaphore(K)
  → 同时最多 K 个真实 API 请求
  → asyncio.gather 保持输入顺序收集
  → 输出耗时、吞吐、峰值并发、失败数
```

`K` 的默认值：Ollama 单卡为 1，DeepSeek 云端为 2；可用 `LLM_MAX_CONCURRENCY` 覆盖，范围限制为 1～16。同一事件循环内的所有批次共享一个 Semaphore，而不是每个批次各建一扇互不相干的门；运行中不能动态切换上限，修改后应重启服务。2.3.1 起同步/异步入口还共享 `LLM_REQUESTS_PER_MINUTE` 请求日志；详细原因见 `docs/RATE_LIMITING_2_3_1.md`。

## 优化前后对比

### 可重复 I/O 模拟：8 个任务，每个等待 250ms

| 方案 | 总耗时 | 吞吐 | 峰值并发 | 相对加速 |
|---|---:|---:|---:|---:|
| 串行基线 | 2004.1ms | 3.992 req/s | 1 | 1.00x |
| async + Semaphore(2) | 1000.8ms | 7.994 req/s | 2 | 2.002x |

### 本机 Ollama：4 个约 64 token 请求，`qwen2.5:3b`

| 方案 | 总耗时 | 吞吐 | 峰值并发 | 相对加速 |
|---|---:|---:|---:|---:|
| 串行基线 | 2277.0ms | 1.757 req/s | 1 | 1.00x |
| async + Semaphore(2) | 2279.6ms | 1.755 req/s | 2 | 0.999x |
| async + Semaphore(4) | 2284.8ms | 1.751 req/s | 4 | 0.997x |

结论：异步对可并发的网络等待接近 2 倍增益，但本机单 GPU Ollama 基本不加速；并发提高到 4 还略有额外开销。因此本项目本地默认 1，云端批处理默认 2，并要求用实际后端压测后再调大。

## 适合继续异步化的地方

1. 云端 LLM 批量评测、多人格独立候选、批量摘要：任务互不依赖，使用 `acomplete_batch()`。
2. RAG 查询改写与 HyDE：两者互不依赖，可在未来 async Retriever 中并发，但仍要共用 LLM Semaphore。
3. RAG 与联网搜索预取：两路上下文互不依赖，可并发等待；建索引应缓存，不能每轮重复。
4. 多用户 API 服务：使用 ASGI/FastAPI + `acomplete()`；不要在 async handler 内调用同步 `complete()` 阻塞事件循环。

## 不应异步化的地方

- 单篇正文的依赖步骤、Reviewer 与发布门禁。
- Playwright 真实发布和账号登录。
- Cross-encoder、PDF/图片处理等 CPU/GPU 密集任务；应使用批处理、进程池或独立推理服务。
- 本机单卡 Ollama 的大量并发请求；会争抢同一 GPU，并不等于并行推理。

## 复现命令

```bat
python scripts\benchmark_async_llm.py --mode simulated --tasks 8 --concurrency 2 --delay-ms 250
python scripts\benchmark_async_llm.py --mode ollama --tasks 4 --concurrency 2 --model qwen2.5:3b --max-tokens 64
```

详细原始结果：`docs/ASYNC_BENCHMARK_SIMULATED.json`、`docs/ASYNC_BENCHMARK_OLLAMA.json` 和 `docs/ASYNC_BENCHMARK_OLLAMA_C4.json`。
