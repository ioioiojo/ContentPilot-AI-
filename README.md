# ContentPilot 内容运营工作台

面向受控小团队的内容运营上线候选版（0.7.0）：关键词参考帖、AI 二创、选题、生成、自动审核、人工确认、素材、发布记录、效果分析及下一轮反馈。本地模板默认 Mock；生产模板要求真实模型、PostgreSQL 和 HTTPS。当前本机已使用 flux-code.cc 的 gpt-5.5 文字接口。Mock 分数不代表内容质量或业务收益。

本版增加管理员「服务配置」、本地知识库检索、真实热点网关、独立审稿分配、生成断点续跑、运营数据定时采集和发布素材选择。图片、视频、发布、热点及完整运营指标仍需可用供应商/平台权限；未配置时明确禁用，不使用模拟数据替代。使用入口和服务协议见 [功能与配置说明](deployment/FEATURES.md)，上线验收见 [LAUNCH.md](deployment/LAUNCH.md)。

「参考帖二创」支持关键词搜索、手动录入笔记、最多选 5 条结合商品事实生成原创稿，记录来源快照并检测长段照搬。0.6.1 已实现 TikHub 小红书搜索和正文读取适配，保留原搜索网关；当前缺少 TikHub 密钥，未进行真实搜索验收，页面明确显示未接通。使用步骤和配置见 [搜索与二创说明](deployment/REFERENCE_SEARCH.md)。

## 已实现

- 中文网页：登录、商品资料、任务列表、生成版本、审核编辑、审核记录、JSON 导出、个人统计、成员管理。
- 每个账号只访问自己的商品、知识资料和任务。管理员管理账号、审稿资格与服务；审稿人只能访问所有者明确分配的任务，审核审计保留实际操作者。
- Cookie 会话、密码哈希、退出/修改密码/停用账号撤销会话、CSRF、同源校验、请求大小限制、登录限速。
- FastAPI 立即返回排队任务；独立 worker 执行 LangGraph 流程，保存步骤、耗时、初稿、修改稿和审核结果。
- 数据库原子领取任务、租约、失效恢复、子进程硬超时、有限重试；最终状态始终进入人工审核。
- 商品事实和来源快照；场景种草、购买决策、使用指南三种版本化业务技能；默认经真实 stdio MCP 客户端调用本地话题与规则服务。话题仍是示例，不是实时热点。
- 已审核内容可登记平台链接，或通过已配置、已授权的 HTTPS 网关发布；保存内容版本，重复请求不重发，结果不明需人工核对。
- 人工质量标注、标注集 JSONL 导出、自动评测 CLI；人工通过率/事实错误率与模型自评分分开。
- 曝光、点击、成交、营收和总成本累计快照；CTR、转化率、ROI 及同平台版本观察对比。新任务可参考同账号、同商品的历史效果。
- 后台生成 AI 图片提示词和视频分镜；图片 PNG 与视频 MP4 支持可选服务接入、下载和异常状态。素材失败不自动重复付费请求。
- SQLite 本地运行；PostgreSQL、web、worker 的 Linux Docker Compose 部署配置。应用独占服务器本机 18080 端口，不占用已有网站 80/443。

## 本地运行（Windows PowerShell）

第一次安装：
~~~powershell
cd C:\Users\Administrator\Desktop\xiangmu
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements.lock --index-url https://pypi.org/simple
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m contentpilot_agent.manage init-db
.\.venv\Scripts\python.exe -m contentpilot_agent.manage create-admin admin
~~~
如果已有 .env，请保留现有配置，不要覆盖。创建管理员时交互式输入 12–128 位密码，没有默认密码。

启动网页（终端 1）：
~~~powershell
.\.venv\Scripts\python.exe -m uvicorn contentpilot_agent.api:app --host 127.0.0.1 --port 8000
~~~
启动后台任务（终端 2，同一项目目录）：
~~~powershell
.\.venv\Scripts\python.exe -m contentpilot_agent.worker
~~~
打开 http://127.0.0.1:8000 ，登录 → 商品资料 → 添加商品 → 新建任务（选择内容技能）→ 等待生成 → 编辑/审核。审核通过后在任务详情中点击「生成素材 / 分镜」或「登记 / 发布内容」；发布记录点击「录入效果」，在左侧「发布与效果」查看指标。未启动 worker 时任务会停留在排队状态。

只想测试文字 AI：填写真实商品事实，生成并人工核对即可。测试运营闭环：手动发布后登记真实链接 → 输入同口径累计数据 → 新建同商品任务并保留「参考历史效果」。不要把测试数据当成真实业绩。

后台任务依赖本地 data/contentpilot.db，重启前后保持同一工作目录。旧版根目录 contentpilot.db / RunStore 不自动迁移到用户隔离数据结构，旧数据可保留归档。

升级已有版本：先停止本项目web/worker，运行以下命令（备份路径须未存在），然后按上面的命令重启。不要覆盖.env或重新创建账号。
~~~powershell
.\.venv\Scripts\python.exe -m contentpilot_agent.manage backup-local artifacts/pre-upgrade.db
.\.venv\Scripts\python.exe -m contentpilot_agent.manage init-db
~~~
备份使用SQLite backup API并检查完整性，包含WAL中的已提交数据；数据库含私人内容和密码哈希，应限制备份的访问权限。

## 测试与打包

~~~powershell
.\.venv\Scripts\python.exe -m playwright install --no-shell chromium
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check contentpilot_agent tests
.\.venv\Scripts\python.exe scripts/package_release.py
~~~

浏览器未安装时浏览器测试会跳过，不能把跳过当作验证通过。部署包使用文件白名单，不包含 .env、数据库、账号、缓存、虚拟环境或个人简历。打包后输出 SHA-256。

服务器上传步骤见 [部署说明](deployment/README.md)。本机未安装 Docker 时不能声称已验证 Linux 容器启动；测试证据见 [验证记录](docs/VALIDATION.md)。

## 模型与外部服务

保持 CONTENTPILOT_PROVIDER=mock 不会因为存在 API Key 而自动付费调用。准备好后改为 compatible，并填写 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL，然后重启 web 与 worker。真实模式配置缺失会明确失败，不会默默切回 Mock。

每次模型请求超时 45 秒，SDK 不自动重试；新任务默认最多执行 1 次，可通过 JOB_MAX_ATTEMPTS 设置最多 3 次，旧任务保留创建时预算。一次工作流包含多次模型请求和最多 3 轮修改，任务默认 180 秒总时限。每账号滚动 24 小时新建文字任务默认最多 50 个，素材最多 20 个；数量限额不等于货币消费上限。

Token 字段来自供应商 usage；费用未配置时为 null。图片、视频和发布网关需额外的服务授权及配置；仅配置文字模型不会自动拥有这些能力，参见 [集成协议](deployment/INTEGRATIONS.md)。素材需人工检查后自行使用，当前发布协议只发送文字与来源快照，不自动附加素材。

离线评测默认不付费：
~~~powershell
.\.venv\Scripts\python.exe -m contentpilot_agent.evaluation --provider mock
~~~
指定 `--provider compatible` 才使用已配置的真实模型并产生调用；可传 `--cases 文件.json` 自定义测试商品及 forbidden_claims。自动规则通过不代表事实核验或业务效果。

## 结构与边界

- api.py / templates / static：接口与网页。
- database.py / security.py：用户、商品、任务队列、会话和审核审计。
- worker.py / workflow.py：租约执行与 LangGraph 编排。
- providers.py / quality.py：模型适配器、JSON 校验、确定性规则。
- tools.py / mcp_server.py：本地参考话题和 stdio MCP 服务。
- skills.py / growth.py：业务技能与效果反馈。
- operations.py / publishing.py / media.py：评测、发布及素材任务。
- manage.py：初始化数据库、创建管理员、重置密码。
- compose.yaml / Dockerfile / scripts：部署、备份恢复与打包。

来源字段用于追溯商品事实，不自动抓取 URL。知识库使用本地关键词检索；真实热点、图片、视频、发布及完整运营采集需外部服务，TikHub 仅提供可获得的公开互动数据。系统不提供独立事实核查、完整广告合规保障、高可用或随机流量 A/B 实验。schema v1/v2/v3/v4→v5 仅新增表，升级前须备份；服务密钥加密，必须同时保留数据库与配置加密密钥。目标服务器尚未实测。

## 简历表述

作为个人项目可描述：使用 Python、LangGraph、FastAPI 构建内容运营工作台，完成用户隔离、商品资料快照、后台任务、规则审核与人工审批；通过数据库租约、子进程超时、有限重试和版本冲突校验处理运行及审核异常，提供 Linux Docker Compose 部署包。具体上线效果和模型质量需要在真实环境取得数据后再填写。
