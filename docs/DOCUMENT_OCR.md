# 扫描件导入：P0 用法与交付边界

日期：2026-10-28。此功能是未发布的本地导入增量；版本号仍为 2.8.1。不包含全栈上传页面。

## 1. 处理流程

`受信本地文件 → 页级分流 → 文本提取 / CPU OCR → 清洗与质量标记 → 本地预览 → 人工确认 → 目录事务提交 → 检索引用`

- PDF 原生文本直接保留；无文字、异常文字或带栅格图像的页进入 OCR。图像检测包括 XObject 和 inline image，图像资源检测偏保守，带装饰图也可能触发 OCR。
- 已有 OCR 隐藏文本层但仍含扫描图的 PDF 会重新 OCR；不因存在文本层就认定准确。
- 可证明没有内容和注释的空白页跳过；视觉识别时只有近乎均匀白色页才记录为空白。没有识别出文字的非空页报错，不静默丢弃。
- PNG/JPEG 校验文件头，并在子进程中核对真实格式、像素和帧数。不支持 DOCX、图片批量包、加密 PDF 或任意 URL 下载。
- 清洗保留原文语义、金额、小数、单位、否定词和缩进；不让 LLM 修补“看起来不通顺”的识别结果。不删除可能影响事实的重复行。

原生 PDF 使用 pypdf；图像渲染使用 pypdfium2；中文/英文印刷体使用 PaddleOCR + PaddlePaddle CPU，含文档方向和文字行方向模块。没有调用 Ollama 或云 OCR。

契约见 `core/types.py` 的 `DocumentParseOptions`、`DocumentParseResult`、`DocumentPageReport`、`OCRBlock`、`OCRPageResult`；能力清单见 `config/ocr_manifest.json`。

## 2. 独立安装与模型准备

在仓库根目录使用 Windows CMD。OCR 虚拟环境和模型目录位于项目所在盘，均不进入 Git/Docker；原应用虚拟环境不安装 Paddle 依赖。

```bat
.venv\Scripts\python.exe -m pip install -e ".[ingest]"
.venv\Scripts\python.exe -m venv .venv-ocr
.venv-ocr\Scripts\python.exe -m pip install --no-cache-dir -r requirements-ocr.txt
.venv-ocr\Scripts\python.exe scripts\prepare_ocr.py --download-models
```

准备命令仅从固定的 Paddle 官方 HTTPS 模型地址下载四个模型，限制包大小，不执行下载脚本，不提取归档中的任意路径/链接。完整目录发布后记录模型文件哈希和依赖版本。

导入期间验证模型内容及运行版本，不再下载。后续修改模型文件、增加额外文件或更换运行依赖会失败；升级时准备新的模型目录。SHA-256 用于后续完整性检查，不等于独立校验了首次下载的发布者签名。

`requirements-ocr.txt` 固定本次验证的直接依赖；不是包含所有传递依赖和哈希的完整锁文件。当前验收平台为 Windows/Python 3.11，Linux OCR 镜像未验收。

## 3. 先预览再导入

将受信资料放到操作者指定目录。下面用 Git/Docker 忽略的 `.tmp/documents` 示意；预览会主动输出文档正文，不要复制到公开日志。

```bat
if not exist .tmp\documents mkdir .tmp\documents
.venv\Scripts\python.exe main.py knowledge --source-root .tmp/documents inspect --file guide.pdf --ocr --ocr-python .venv-ocr/Scripts/python.exe
```

输出包含 `units` 与 `pages`，可核对识别文字、页号、文字框、置信度和质量标记。该命令不写目录版本；默认不保留页图或 OCR 正文副本。需要保存预览时，仅重定向到受控的本地忽略目录。

核对输入原件和预览后执行：

```bat
.venv\Scripts\python.exe main.py knowledge --source-root .tmp/documents ingest --file guide.pdf --ocr --ocr-python .venv-ocr/Scripts/python.exe --accept-ocr-review
.venv\Scripts\python.exe main.py knowledge search --query "资料中的关键问题"
```

- 默认模型目录为 `.rag_cache/ocr-models`，可由本地操作者通过 `--ocr-model-root` 指定。
- `--force-ocr --ocr` 强制重新识别所有 PDF 页，适用于文本层看似正常但实为错码的资料。
- `--accept-ocr-review` 是操作者对当前导入结果的明确确认，不是自动准确率认证、审批令牌或签名。预览后不得更换文件或识别配置再直接确认；浏览器端绑定内容版本的审核流程属于 P1。
- 未确认的 OCR 导入返回 `ocr_review_required`，退出码 1，旧版本不变。
- 全局 `--db / --source-root / --kb-id` 在 `inspect/ingest/search` 之前；OCR 参数在 `inspect/ingest` 之后。

`inspect` 的输出包含完整正文，属于显式本地查看；`ingest` 和任务日志只输出计数、哈希、ID 与固定原因码。

## 4. 位置与质量信息

原生页保留页号；OCR 块另带：

| 字段 | 含义 |
|---|---|
| `extraction_method` | `native` 或 `ocr` |
| `bounding_box` | `[x0,y0,x1,y1]`，显示页左上角为原点、归一化到 0–1；不是 PDF 底部原点坐标 |
| `ocr_confidence` | 引擎给出的块置信度，不是准确率，也不是检索 score |
| `ocr_engine` | OCR/Paddle 运行版本 |
| `ocr_model_fingerprint` | 模型内容、来源及运行版本清单的指纹 |
| `quality_flags` | 保留初始质量风险，不因人工确认而删除 |
| `ocr_reviewed` | 本地操作者明确确认过当前导入 |

全部 OCR 块都有 `ocr_unverified`、`layout_unverified`；低于默认 0.85 的另有 `low_confidence`，异常字符比例另有 `suspicious_text`。0.85 是初始工程提示阈值，尚未用独立数据校准。人工确认后 `ocr_reviewed=true`，仍保留初始标记。

这些字段随 Chunk、目录快照和问答 `sources` 传递。方向纠正后的文字框转换回原始显示页，便于后续前端高亮。P0 尚未增加浏览器 PDF 阅读器或高亮按钮。

## 5. 资源与失败行为

| 限制 | 默认值 |
|---|---|
| 原文件 | 5 MiB |
| PDF 页数 | 100；同时受像素总预算限制 |
| 正文 | 500,000 字符 |
| OCR 块 / 最终 Chunk | 各最多 2,000 |
| PDF 渲染 | 150 DPI |
| 单页 / 总渲染像素 | 12,000,000 / 60,000,000 |
| OCR 子进程执行超时 | 120 秒，可显式设定 1–600 秒 |
| 推理 | CPU、2 线程；每份文档启动独立进程 |

OCR 子进程不继承 API Key 等应用配置；推理阶段禁止 Python socket 连接，只读取已校验本地模型。stdout/stderr 不进入公开输出，临时结果通过受限文件读取后清理。超时只终止本次启动的 OCR 进程及其子进程，不扫描或结束其他 Python/浏览器/Ollama 进程。

这不是操作系统沙箱：没有容器级内存限制，也不保证原生 pypdf 提取具有强制进程超时。只处理受信资料，恶意 PDF 的完整隔离要在后续 Worker/容器阶段验收。并行启动多个 CLI 也没有跨进程全局 Semaphore。

任一页识别失败、模型异常、超时、超限或未确认都不提交新文档。原有事务、并发版本检查和旧版本可用性保持有效。解析/分块版本、OCR 策略与模型指纹参与版本计算。

常见原因码：`pdf_page_requires_ocr`、`ocr_models_invalid`、`ocr_runtime_mismatch`、`ocr_timeout`、`ocr_pixel_limit`、`ocr_empty_page`、`ocr_review_required`。具体源路径、OCR 全文和异常栈不写任务失败原因。

## 6. 验收与下一步

离线契约与回滚测试：

```bat
.venv\Scripts\python.exe -m pytest tests/test_document_ocr.py tests/test_knowledge_base.py -ra
```

真实模型自制样例（主动生成非个人资料，需已准备模型）：

```bat
.venv\Scripts\python.exe scripts\ocr_acceptance.py
```

默认使用系统中文字体；其他系统可传 `--font`。样例、数据库、预览与报告写到 `.tmp`。公开测量只保留固定案例标识、计数、CER、原因码、耗时和模型指纹。

CER 仅去空白后计算编辑距离；不删除标点来美化结果。当前材料是少量自制印刷体，不能代表独立扫描件质量；固定样例阈值不等于行业标准。真实记录见 [验证记录](TEST_RESULTS.md) 与 [本次 OCR 报告](OCR_P0_ACCEPTANCE.json)。

P0 不保证手写、表格单元格、公式、复杂多栏顺序、图片事实理解或扫描件脱敏。不会因支持 OCR 而自动扩大隐私扫描器的覆盖声明。

P1 的闭环是：受控上传 API、持久化后台任务、页面预览/修订、绑定文档版本的确认、问答引用跳转；之后再做独立资料集、页面权限和容器复现。现有真实发布权限保持不变。
