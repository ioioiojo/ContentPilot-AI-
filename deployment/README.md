# Linux 服务器上传与部署

目标服务器已有网站。本项目只监听服务器本机 127.0.0.1:18080，PostgreSQL 不开放主机端口。用独立子域名接到已有反向代理，保留原网站的服务和配置。版本 0.7.0；上线范围、限额和验收门槛见 [LAUNCH.md](LAUNCH.md)。参考帖和搜索服务见 [REFERENCE_SEARCH.md](REFERENCE_SEARCH.md)。

## 1 上传准备好的包

本机先运行：
~~~powershell
cd C:\Users\Administrator\Desktop\xiangmu
.\.venv\Scripts\python.exe scripts/package_release.py
scp .\dist\contentpilot-0.7.0.tar.gz USER@SERVER:/tmp/
~~~
USER、SERVER 换成自己的账号和服务器地址。然后登录服务器，将包解压到新目录：
~~~sh
mkdir -p ~/apps/contentpilot
tar -xzf /tmp/contentpilot-0.7.0.tar.gz -C ~/apps/contentpilot
cd ~/apps/contentpilot
sh scripts/prepare_server_env.sh
~~~
脚本生成随机十六进制数据库密码，已有 .env 时拒绝覆盖。用编辑器把 .env 中 ALLOWED_HOSTS 的 contentpilot.example.com 换成实际子域名，保留 localhost,127.0.0.1。生产配置要求 APP_ENV=production、CONTENTPILOT_PROVIDER=compatible、模型密钥和模型名称；按用户指定配置 OPENAI_BASE_URL=https://flux-code.cc、OPENAI_MODEL=gpt-5.5。若只做无付费预演，明确改为 APP_ENV=staging、CONTENTPILOT_PROVIDER=mock。不要执行或 source .env；Compose 负责读取。

## 2 构建启动

服务器须已安装 Docker Engine 和 Compose v2。推荐至少给本项目预留 2 核/3 GB 可用内存及数据库磁盘空间；已有网站的资源需求另算。镜像拉取与 Python 包安装需要网络可用。

~~~sh
sh scripts/deploy.sh first
docker compose ps
curl -fsS http://127.0.0.1:18080/health
curl -fsS http://127.0.0.1:18080/ready
docker compose logs --tail=100 web worker
docker compose exec web python -m contentpilot_agent.manage create-admin admin
~~~
创建账号时交互式输入密码。健康响应应为 {"status":"ok"}，就绪响应为 {"status":"ready"}。脚本初始化 schema v5，worker 等 web 健康后启动，PostgreSQL 数据保存在命名卷中；网页服务配置的加密密钥保存在共享 service_keys 卷。deploy.sh 需要 Linux 上的 flock 和 curl。服务管理、采集和审稿操作见 [FEATURES.md](FEATURES.md)。

不要用 docker compose down -v 清理正常部署，这会删除数据库卷。模型切换时重建容器保留命名卷即可。

## 3 接入现有反向代理

建议建立 contentpilot.你的域名。DNS 指向同一服务器，沿用当前证书管理方式申请该子域名的证书。

Nginx 示例在 nginx-location.example：这是独立 HTTPS server block，替换域名和证书路径再加入现有配置；不要覆盖现有网站 server block。若用 Caddy，可在现有 Caddyfile 中添加：
~~~text
contentpilot.example.com {
    reverse_proxy 127.0.0.1:18080
}
~~~
修改前先备份配置；Nginx 先 nginx -t，检查通过后 reload。Compose 不自带反向代理，也不申请证书，不抢占 80/443。

当前网页使用 /api 和 /static 根路径，因此只支持子域名根路径部署，不直接挂载到现有站点 /contentpilot/ 子路径。COOKIE_SECURE 在 Compose 固定为 true，用户应经 HTTPS 访问；直接 HTTP 端口仅用于 /health 检查。

如果现有反向代理也在容器里，它的 127.0.0.1 不是宿主机。需要由管理员将 web 与反向代理接入一个专用外部 Docker 网络，通过 web:8000 访问；本示例默认反向代理运行在宿主机，不能不加调整照搬。

## 4 首次验收

- 管理员登录，创建成员账号；成员录入商品事实和来源。
- 创建任务后立刻看到排队/执行状态，worker 完成后为待人工审核。
- 编辑一条内容并通过，导出 JSON 包含人工版本及资料来源。
- 另一个账号看不到该用户的资料/任务；退出、停用和改密后旧会话失效。
- 重启 web/worker 后历史数据仍在；旧执行租约过期后任务最多按执行预算重跑。
- 自动审核未通过也进入人工审核；规则阻断问题须先编辑修复才能批准。

静态配置检查与本地 SQLite 测试不等价于这套 Linux/PostgreSQL 容器验收。首次部署需完成以上操作后再邀请团队使用。

## 5 备份恢复及更新

~~~sh
sh scripts/backup.sh
~~~
备份以受限权限存入 backups/contentpilot-UTC时间-进程号.dump，格式为 PostgreSQL custom archive。脚本先检查 pg_dump 成功后才生成最终文件，不自动删除旧备份。按 LAUNCH.md 安排每日备份、监控和异地复制。

在维护窗口执行恢复（先备份当前数据）：
~~~sh
sh scripts/restore.sh --confirm-replace-database backups/contentpilot-时间.dump
~~~
恢复脚本先检查归档结构，停止本项目 web/worker，备份当前数据库，再通过事务清理并恢复归档中的表，成功后重新启动并检查 /ready。失败会回滚并保持应用停止以便检查。只恢复自己控制的可信备份，第一次使用须在单独测试部署里演练。

升级前备份，保留旧包与旧镜像，解压新代码到项目目录时不要覆盖 .env，再执行：
~~~sh
sh scripts/deploy.sh upgrade
~~~
0.7.0 使用 schema v5；从 v1/v2/v3/v4 升级只新增缺少的表，保留原有账号、商品、任务和审核记录。脚本构建和预检通过后，停止旧 web/worker，再备份、初始化及启动新版本。未知 schema 版本会阻止启动。回滚旧代码前须恢复升级前数据库和配对 key 备份，不能只降版本号。首次上线还须验证 PostgreSQL 上迁移与恢复。已有 .env 须保留 APP_ENV、限额和 JOB_MAX_ATTEMPTS，不覆盖原密钥；可从管理员页面配置可选服务。备份同时复制 .dump 和存在时的 .dump.key 到受控异地存储，不删除 service_keys 卷。

回滚时使用当前恢复脚本的停机模式，避免新版本启动后再次迁移旧数据库：
~~~sh
sh scripts/restore.sh --confirm-replace-database backups/升级前备份.dump --keep-stopped
~~~
然后恢复匹配的旧代码/Compose 文件及旧镜像（保留 .env 和数据库卷），再启动对应版本。回滚会舍弃升级后写入的数据；恢复脚本额外生成的当前数据库备份可用于追溯，不能未经检查合并新旧数据。恢复旧数据库时新增的心跳表可能保留，但版本号和旧业务数据来自备份，旧代码只检查旧 schema 版本。

可选图片、视频及授权发布服务见 [集成协议](INTEGRATIONS.md)。未配置时禁用对应功能；手动登记链接不等于系统自动发布。

## 6 后续接模型

在服务器 .env 中设 CONTENTPILOT_PROVIDER=compatible 并配置 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL，再 docker compose up -d。新任务记录所选 provider；已经排队的 Mock 任务仍按 Mock 执行。凭据不存到任务记录或导出文件，不在客户端输入模型密钥。

真实模型第一次接入后，应重新验证输出结构、数量、失败重试、耗时、Token 用量及审核质量。当前 Mock 的 68/88 分为预设演示数据。
