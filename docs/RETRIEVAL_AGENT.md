# 受控检索 Agent

默认入口仍为固定 Workflow；请求设置 `execution_mode=agent` 时启用模型驱动的只读检索循环。发布流程和权限不变。

## 行动协议

模型返回单个 JSON 对象，支持一个完整 json code fence。Schema 拒绝额外字段。

```json
{"action":"search","query":"Semaphore concurrency"}
```

其他动作：

- `{"action":"finish"}`：已有证据足够，交由回答和校验阶段处理。
- `{"action":"clarify","clarification":"需要比较哪两个方案？"}`：返回澄清问题，等待用户下一轮。
- `{"action":"insufficient_evidence"}`：证据不足，不生成事实结论。

模型只选择 query。工具名、库权限、路径、身份、top_k 和 min_score 均由程序提供；没有发布、Shell、任意文件或网络工具。

实现位于 `core/retrieval_agent.py`，经 `assistant.py` 注入受控工具和模型调用器。默认实际检索经过 StructuredTool → Gateway → Harness。协议是结构化 JSON 行动，不是服务商原生 tool_calls；当前循环也不是 StateGraph 或多 Agent。

## 循环与预算

1. 将目标、裁剪后的历史和工具观察作为不可信 JSON 数据送入模型。
2. 验证行动契约，经网关执行搜索，合并有效来源并统一引用编号。
3. 将结果回传模型，继续查询、澄清或结束。
4. 检查回答后返回；完成的对话可按既有设置写入 SQLite。

最多三次检索。归一化后重复查询直接停止；有证据后没有新增来源，仅允许一次收尾决策，不再执行搜索；连续两次空结果停止。

`max_steps` 计算模型决策、工具执行和生成动作。UI 为 8，HTTP 默认仍为 4，复杂补查需显式传 8。引用检查是本地安全步骤，不能因预算耗尽跳过，因此 Trace 行数可能大于 budget_steps。

Token 是累计输入/输出估算，不是精确计费硬上限。摘录最多累计 12 个来源、6,000 字符。单次模型调用不做隐藏重试；决策默认 20 秒，并受本轮剩余时间约束。

同步检索在线程中安全收尾，不承诺强制杀线程或严格墙钟截止。取消模型调用时关闭本次异步响应；服务商是否停止计算或计费不由本项目保证。

## 结束与降级

| stop_reason | 处理 |
|---|---|
| evidence_ready | 证据就绪，摘录或生成后检查 |
| needs_clarification | 返回澄清问题，不生成事实回答 |
| insufficient_evidence、finish_without_evidence | 明确缺少证据 |
| repeated_query、no_new_evidence、search_limit | 停止搜索，材料不完整时声明限制 |
| planner_invalid_fallback | 有预算且尚未搜索时，以原问题检索一次并摘录 |
| decision_timeout、deadline、budget_exhausted | 停止，不继续调用模型 |
| tool_blocked、catalog_not_allowed | 不扩大权限、不降低阈值 |
| grounding_fallback、answer_unavailable | 生成不可用或检查未通过，回退证据 |

完整停止码以代码为准。内部异常只记录类型，不向回复暴露异常正文、路径或凭据。

## 回答与会话

`answer_mode=extractive` 不调用回答模型，但 Agent 的行动决策仍调用模型。`llm` 在证据足够后综合生成，检查失败回退摘录。跨文档比较至少要求两个独立来源。

Agent 输出检查后分块，不是真实逐 Token 生成流。词法引用支持度可能误拒同义表述，不能证明语义事实性。

澄清是一次正常回复；开启会话恢复时，用户下一轮补充作为历史进入新循环。Checkpoint 不保存循环中途状态，没有中途续跑能力。

新增审计只含动作、计数、哈希、耗时和停止码；既有显式记忆与完成对话的本地保存设置不变。

## 使用与验证

工作台选择“受控检索 Agent”、已入库目录、Qwen2.5:7b 和“原文证据”。来源、Trace、停止原因及估算用量可在消息下方查看。3B 不再作为助手选项，未删除模型文件。

```sh
python -m pytest tests/test_retrieval_agent.py tests/test_catalog_ui.py
python -m eval.retrieval_agent_scorer
python scripts/retrieval_agent_smoke.py --model qwen2.5:7b --report-json logs/retrieval_agent_smoke.json
```

最后一条是主动本机模型验证，不下载模型、不调用云端、不读取已有用户目录库。脚本创建两份临时教学资料，完成后仅清理自己创建的目录。默认测试和门禁不自动执行该模型请求。

[合成验收](RETRIEVAL_AGENT_ACCEPTANCE.json) 包含 8 条脚本决策/合成检索用例，只验证控制流。[本机诊断记录](RETRIEVAL_AGENT_SMOKE.json) 合并保留五次运行，包括失败：

- 3B 首次超时；后续仍存在重复检索和收尾不稳。
- 7B 两次检索后 finish，返回两个文档；模糊问题 clarify。比较约 13 秒，含首次调用；澄清约 0.5 秒，最终使用摘录。
- 7B 综合生成另一次触发 grounding_fallback，约 8.83 秒；未放松校验来使其通过。

这些题目为开发诊断，不是独立人评。当前使用 7B + 原文证据；综合生成、独立质量、其他规模和新容器仍需后续验收。
