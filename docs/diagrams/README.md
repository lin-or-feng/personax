# PersonaX 流程图图片

更新日期：2026-10-15。由 [项目架构与流程图](../知识图谱.md) 的 Mermaid 源码生成，发布基线 2.8.1，第二阶段为未发布工作区增量。CLI/API/UI 同源接入、Qdrant 本地适配与实体共现图已实现；真实模型仅有小规模工程冒烟，不代表独立语义质量、专业因果图谱或生产部署验收。

| 图片 | 讲解用途 | 文件 |
|---|---|---|
| 总体架构 | CLI/API/UI 同源问答与新旧数据源边界 | [PNG](architecture.png) · [SVG](architecture.svg) · [源码](architecture.mmd) |
| 内容与发布 | 生成、审核、发布约束、AI 声明和成功证据 | [PNG](content_publish.png) · [SVG](content_publish.svg) · [源码](content_publish.mmd) |
| 文档入库 | 稳定身份、版本、事务、失败保留旧数据 | [PNG](knowledge_ingest.png) · [SVG](knowledge_ingest.svg) · [源码](knowledge_ingest.mmd) |
| 证据检索 | 有效快照、召回、门槛、来源定位与无证据返回 | [PNG](knowledge_search.png) · [SVG](knowledge_search.svg) · [源码](knowledge_search.mmd) |
| 删除与中断 | 软删除、遗留任务核查、人工确认后重试 | [PNG](knowledge_recovery.png) · [SVG](knowledge_recovery.svg) · [源码](knowledge_recovery.mmd) |
| 实体关联 | 原文共现、两跳查询、过期关系排除 | [PNG](entity_graph.png) · [SVG](entity_graph.svg) · [源码](entity_graph.mmd) |

PNG 用于嵌入，SVG 用于缩放查看。审批入口、软删除、摘录和模型生成的边界以对应 Markdown 说明为准。

## 重新生成

图源只维护 `docs/知识图谱.md` 中的六个 Mermaid 代码块，不手动修改派生 `.mmd / .svg / .png`。渲染脚本也实际解析和渲染 README 与白皮书中的图。

在项目根目录准备可选文档工具，不改 Python 运行依赖：

```powershell
npm install --prefix .tmp/diagram-tools --ignore-scripts mermaid@11.12.0 playwright
.tmp/diagram-tools/node_modules/.bin/playwright install chromium
node docs/render_project_diagrams.mjs
```

也可用 `--deps-root` 指向已有 Node 包目录，`--playwright-root` 指向已有 Playwright 包。脚本仅在本机回环地址临时提供 Mermaid 静态文件，结束时关闭；不会运行模型、项目服务或真实发布。

仅维护 Markdown 图源及其派生图片；旧 Word 导出与生成器已移除。
