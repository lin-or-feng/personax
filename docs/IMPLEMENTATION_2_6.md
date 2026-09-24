# PersonaX 2.6：可信回答与端到端可观测

## 1. 为什么做这一版

2.5 已能给出来源编号，但“引用编号合法”不代表“引用内容支持这句话”；同时，LLM、工具和最终回复虽然分别写日志，却缺少可按同一轮请求串联的视图。2.6 只解决这两个可验证问题，不引入 Redis、ANN 或新的常驻服务。

## 2. 回答事实支持度

运行时 Reviewer 对回答逐句处理，只对带引用的结论进行证据核对：

1. 提取 `[n]` 引用并校验是否落在当前来源范围。
2. 将中文双字片段、英文术语和数字构成可复现词项。
3. 只和被引用来源的标题及 excerpt 比较，而非和全部召回结果比较。
4. 识别“已经/已实现/已替代”等肯定完成表达与“尚未/没有/不支持”等证据否定表达的明显冲突。
5. 未通过时保留原回答，但标为 `degraded`，追加核对来源提示并写入 Agent Trace。

离线数据集 `eval/grounding_eval_set.json` 包含 8 条支持样例和 8 条不支持样例。发布阈值是 Accuracy ≥ 0.90、unsupported recall ≥ 0.85：

```powershell
& .\.venv\Scripts\python.exe -m eval.grounding_scorer --summary-only
```

这是确定性词法基线。它能发现错引、无效编号和明显否定冲突，但不能可靠理解同义改写、复杂数值推理或跨句蕴含；生产事实性评估仍应补充人工标注、NLI 或独立 Judge，并保留对 Judge 自身偏差的校准集。

## 3. Trace 与遥测数据契约

一轮助手请求生成唯一 `trace_id`，依次传入：

```text
AssistantRequest
  -> Router LLM
  -> StructuredTool / AssistantToolGateway
  -> Answer LLM
  -> Reviewer
  -> assistant_reply
```

`logs/usage.jsonl` 只记录诊断元数据：事件类型、Trace ID、后端/模型/工具、状态、错误类型、尝试次数、来源数量、Token 与耗时。以下内容明确不记录：提示词、回答正文、检索 query、工具参数、知识片段和密钥。

读取器最多加载最近 5,000 条，每行上限 64 KiB；损坏或异常超长记录只计数并跳过，不能让状态页崩溃。

## 4. 聚合口径

- 工具成功率：`status=ok` 工具事件 / 全部工具事件。
- 助手降级率：`degraded=true` 回复 / 全部回复。
- P95：使用最近读取窗口内事件耗时做线性插值，不宣称为长期线上 SLA。
- Token：聚合 LLM 返回的 prompt/completion usage；后端未返回 usage 时为 0，不做伪估算。
- 成本：按 `LLM_INPUT_COST_CNY_PER_MILLION` 与 `LLM_OUTPUT_COST_CNY_PER_MILLION` 计算。未配置时 UI 明确显示“未配置”；该值是估算，不是供应商账单。

查看聚合或指定 Trace：

```powershell
& .\.venv\Scripts\python.exe main.py telemetry
& .\.venv\Scripts\python.exe main.py telemetry --trace-id trace-xxxxxxxxxxxxxxxx
```

可视化工作台的“状态与日志”页提供相同聚合和安全事件表。

## 5. 发布门禁

`scripts/release_check.py` 依次执行七道门禁：完整单测、内容合规、密钥扫描、安全红队、答案事实支持度、助手回归和 RAG 质量。任一步非零立即停止。

```powershell
& .\.venv\Scripts\python.exe scripts\release_check.py
```

所有离线测试都不登录平台、不打开 Playwright、不真实发布，也不要求 Ollama 在线。
