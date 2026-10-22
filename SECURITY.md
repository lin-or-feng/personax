# PersonaX 安全政策

## 支持范围

安全修复优先应用于 `main` 分支的最新版本。历史提交与个人修改版本不单独维护。

## 私密报告漏洞

如果问题可能导致密钥、浏览器登录态、用户内容泄露，或绕过发布确认与合规检查，请不要创建公开 Issue。请通过 GitHub 的 [Private vulnerability reporting](https://github.com/lin-or-feng/personax/security/advisories/new) 私密提交：

- 受影响版本与运行环境；
- 最小复现步骤和影响范围；
- 已尝试的缓解方式；
- 不包含真实 API Key、Cookie、`storage_state.json` 或个人数据的脱敏证据。

维护者确认后会评估影响、准备修复并协调披露。在修复发布前，请避免公开可直接利用的细节。

## 默认安全边界

- Docker 默认只监听 `127.0.0.1`；工作台没有内置登录，API 可选 Bearer Token 也不构成完整身份系统，不应直接暴露公网；
- 容器使用非 root 用户、只读根文件系统、移除 Linux capabilities，并启用 `no-new-privileges`；
- 真实发布默认关闭，必须在 Windows 宿主机经过人工确认和发布安全锁；
- `.env`、浏览器登录态、日志、缓存和本地产物不会进入 Docker 构建上下文；
- Ollama 与 DeepSeek 内容可能包含用户输入，使用者应自行确认模型服务的数据处理政策。

## 公开文件检查

`python scripts/check_privacy.py --strict` 检查当前 Git 可纳入版本的文件，仅输出位置与类别，不回显命中值。简历、渲染产物、个人文档、运行日志和凭据由 Git 与 Docker 排除规则隔离；不要强制添加被忽略的私密文件。

`--history` 单独审查可达历史文本。修复当前文件和添加忽略规则不会抹去历史、远端或其他人的副本。脚本不执行图片 OCR，也不覆盖所有秘密格式或提交作者元数据；发布图像需另做本地 OCR 与元数据检查，且不应将私密材料上传第三方 OCR 服务。本次检查范围见 [验证记录](docs/TEST_RESULTS.md)。历史中发现凭据时应先撤销/轮换，再协调清理，不能仅删除工作文件。

本政策不能替代独立渗透测试、镜像漏洞扫描或生产环境的身份认证、TLS、网络隔离与密钥管理。
