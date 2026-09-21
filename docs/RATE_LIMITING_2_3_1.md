# PersonaX 2.3.1 滑动窗口与 Semaphore 设计

## 两种限流解决不同问题

- **滑动窗口**回答“最近 60 秒启动了多少次”。它保护 API RPM、工具预算和发布频率。
- **Semaphore**回答“此刻有多少个还没完成”。它保护连接池、GPU、浏览器会话和瞬时资源。

执行顺序固定为：

```text
请求 → 等待滑动窗口额度 → 获取 Semaphore 槽位 → 发起真实调用
     → 完成/失败后释放槽位 → 需要重试时重新经过滑动窗口
```

必须先等频率额度再占 Semaphore，否则任务会拿着稀缺并发槽位等待几十秒，阻塞已经有额度的请求。

## 当前应用范围

| 资源 | 滑动窗口 | Semaphore | 默认值 | 为什么 |
|---|---|---|---|---|
| 同步 LLM / Streamlit 流式 | 请求启动频率 | 线程 `BoundedSemaphore` | 60 RPM；Ollama=1、云端=2 | 当前 UI 实际走同步接口，也必须防单卡/云端突发 |
| 异步 LLM / 批处理 / 异步流 | 同一进程共享请求频率 | 事件循环级 `asyncio.Semaphore` | 60 RPM；Ollama=1、云端=2 | 独立 prompt 可并发等待，但不能无界创建真实请求 |
| Skill / Assistant 工具 | 每用户调用窗口 | 不加 | 60 次/60 秒 | 调用很短，主要防 Agent 循环或界面重跑消耗预算，不需要占并发槽 |
| 联网搜索增强 | 每后端请求窗口 | 线程 `BoundedSemaphore` | 10 RPM；并发 2 | 属于可选外部 I/O；额度或槽位不足时直接降级，不阻塞正文生成 |
| 小红书真实发布 | 独立发布窗口 | 进程级 `Semaphore(1)` | 3 次/60 秒；单飞 | 有账号副作用且浏览器状态不可安全并行 |

环境变量：

```env
LLM_MAX_CONCURRENCY=1
LLM_REQUESTS_PER_MINUTE=60
WEB_SEARCH_REQUESTS_PER_MINUTE=10
WEB_SEARCH_MAX_CONCURRENCY=2
```

本机 4060 Ti + Ollama 保持并发 1。之前的本机实测中，并发 2/4 没有提高总吞吐；提高 Semaphore 只会让请求争抢同一 GPU。云端 API 可从 2 开始压测，再根据服务商 RPM/TPM 和错误率调整。

## 对话上下文滑动窗口

对话历史与请求频率不是同一种窗口。助手每轮从最新消息向前装入：

1. 最多 `context_window_messages=8` 条；
2. 合计最多约 `context_window_tokens=3000`；
3. 单条最多约 `context_message_tokens=750`；
4. 最终恢复为正常时间顺序后写入提示词；
5. 本地 Checkpoint 独立保留 `checkpoint_messages=20` 条。

token 数使用无额外依赖的保守估算：中文约一字一 token，ASCII 约四字符一 token。它不是模型官方 tokenizer，作用是建立稳定上界；需要精确计费时再按具体模型接入 tokenizer。

## 不加 Semaphore 的内容

- **BM25、RRF、相关性门控、Reviewer**：都是本地短计算，加 Semaphore 只增加排队。
- **SQLite Checkpoint**：事务已经保证单次写入完整性；本地轻量使用无需再套异步门。
- **Cross-encoder**：属于 CPU/GPU 计算密集，应使用模型批处理、工作队列或独立推理服务，不应靠 `asyncio` 假装并行。
- **Embedding 建库**：当前按批次调用并使用持久化缓存；本地单卡应减少重复计算，而不是提高请求并发。
- **Playwright 页面步骤**：一个发布流程内部存在严格顺序，只在整个流程外层使用 Semaphore(1)。

## 部署边界

当前滑动窗口、同步 Semaphore 和发布单飞锁都位于 Python 进程内，适合本地 Streamlit、CLI 和单进程服务。若部署多个 Worker 或多台机器，需要把频率事件迁移到 Redis sorted set/token bucket，并用 Redis lock、数据库 advisory lock 或任务队列实现跨进程发布单飞。
