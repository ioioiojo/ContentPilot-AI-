# HTTP 接口

基础路径 /api；浏览器同源 Cookie 会话。除登录外，写请求必须附带登录或 /me 响应中的 X-CSRF-Token。未登录返回 401，权限失败 403，他人资源返回 404，版本冲突返回 409，输入错误返回 422，限速 429。任务属于用户本人，管理员也不能越权读取。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | /login | username、password 登录，返回 user、csrf、provider |
| GET | /me | 当前账号与 CSRF token |
| POST | /logout | 撤销当前会话 |
| POST | /password | current_password、new_password 改密，撤销该用户所有会话 |
| GET/POST | /users | 管理员列出账号 / 创建普通成员 |
| PATCH | /users/{id} | 管理员启用/停用普通成员，active 布尔值 |
| GET/POST | /products | 列表 / 创建商品，name、facts、source |
| PUT/DELETE | /products/{id} | 更新/删除商品，旧任务来源快照不受影响 |
| POST | /jobs | product_id、audience、goal、tone、platforms、content_count、skill_id(scene/decision/tutorial)、use_feedback，返回 202 与排队任务 |
| GET | /jobs?offset=0 | 每页最多 50 条，按创建时间倒序 |
| GET | /jobs/{id} | 输入快照、状态、生成结果、内容版本、步骤事件、人工审核记录 |
| POST | /jobs/{id}/retry | 仅失败且执行次数未耗尽时允许手动重试 |
| POST | /jobs/{id}/review | revision、action(save/approve/reject)、note、contents |
| GET | /jobs/{id}/export | 仅 approved 状态可下载最终人工版本 JSON |
| GET | /stats | 个人状态计数，最近 100 个完成任务的耗时/修改次数/Token |
| GET | /capabilities | 技能、工具模式及素材/网关配置状态；reference_search_service 返回 provider/configured/max_results，不含密钥 |
| GET | /admin/runtime | 仅管理员：版本、运行环境、队列总数量、worker 状态和限额，不读取成员内容 |
| GET/POST | /references | 本人参考库分页列表（offset、keyword）/ 手动录入 title、body、url、author、keyword |
| DELETE | /references/{id} | 删除本人参考帖，旧任务快照仍保留 |
| POST | /references/search | keyword、limit（TikHub 1–5、网关 1–10）、sort_type、note_type；调用已配置搜索服务并整批保存结果，未配置409 |
| GET/POST | /publications | 发布列表 / job_id、revision、content_index、request_key、channel(manual/gateway)、url、experiment、variant |
| GET/POST | /publications/{id}/metrics | 效果历史 / impressions、clicks、conversions、revenue、cost、evidence、captured_at(Unix秒) |
| GET/POST | /jobs/{id}/evaluations | 标注列表 / revision、content_index、facts_score、platform_score、usefulness_score、factual_errors、note |
| GET | /evaluations/export | 本账号人工标注JSONL导出，最多5000条 |
| GET | /growth | 最近1000条发布的效果统计、版本观察对比与人工质量汇总 |
| GET/POST | /jobs/{id}/media | 素材列表 / revision、content_index、kind(storyboard/image/video)、prompt；POST返回202 |
| GET | /media/{id}/download | 仅完成且归本人所有的素材可下载 |
| GET/PUT | /admin/services、/admin/services/{id} | 管理员读取脱敏配置/保存启停、values、clear_secrets 与 revision |
| POST | /admin/services/{id}/test | 管理员显式连接测试，keyword 可选，部分数据接口可能计费 |
| GET/POST | /knowledge | 本账号知识资料列表/新增 title、body、source |
| PUT/DELETE | /knowledge/{id} | 修改/删除本人资料，旧任务摘录保留 |
| GET | /knowledge/search?keyword= | 本账号关键词检索，最多 5 条摘录 |
| POST | /hotspots/search | keyword、platforms，返回真实 topics 与所属账号 batch_id |
| GET | /reviewers | 可分配的启用审稿人，不返回其他账号资料 |
| PUT | /admin/reviewers/{user_id} | 管理员设置 enabled 审稿资格 |
| GET/PUT | /jobs/{id}/assignment | 读取分配/所有者按 revision、reviewer_id 分配或撤销 |
| GET | /review-inbox | 本人被分配的待审核任务 |
| POST | /jobs/{id}/resume | 所有者显式从断点继续，总次数最多 3，返回 202 |
| GET/PUT | /publications/{id}/collection | 采集历史/设置 enabled、interval_minutes（至少 60） |
| POST | /publications/{id}/collect | 本人已确认发布记录立即入采集队列，返回 202 |

发布和素材提交要求当前 approved 版本；人工标注可针对尚待审核内容。素材服务未配置返回409，同账号最多5条未完成素材任务。发布协议和统计口径见 deployment/INTEGRATIONS.md。服务配置仅表示已填写参数，不保证外部接口可用。

0.7.0 POST /jobs 增加 use_knowledge（默认 true）、hotspot_batch_id（本人最近 24 小时批次）。GET /jobs/{id} 和 POST /jobs/{id}/review 对有效的已分配审稿人开放，其他所有者权限不扩展；分配后所有者只能 save，approve/reject 由审稿人操作。发布可选 media_ids（最多 5 个），须属于当前已审核内容和版本，正文/素材幂等匹配不一致返回 409。服务配置详情及外部协议见 deployment/FEATURES.md。

POST /jobs 可选 reference_ids（最多 5 个本人参考帖 ID，不允许重复）、rewrite_guidance（最多 1000 字），服务端保存参考快照。生成和人工批准检查长段原文重复；JSON 导出新增 references/rewrite_guidance。搜索每分钟最多 1 次、滚动 24 小时最多 30 次，不返回模拟帖子，接口协议见 deployment/REFERENCE_SEARCH.md。

TikHub sort_type 支持 general（默认）、time_descending、popularity_descending、collect_descending；note_type 支持不限、普通笔记（默认）、视频笔记。搜索先取 ID 再读正文，失败整批不导入；返回 source 与参考帖 source_type 为 tikhub，旧网关仍为 gateway，手动录入为 manual。原网关只发送 platform/keyword/limit，保持协议兼容。

健康接口 GET /health 位于根路径，仅检查应用与数据库连通性。GET /ready 还要求最近 90 秒内的 worker 数据库心跳，失败返回 503；均不代表模型或外部服务可用。任务创建和手动重试受并发限额约束，新建还受滚动 24 小时额度约束；管理员暂停提交返回 503。

任务状态：queued → running → pending_review → approved 或 rejected。驳回后可编辑再次审核；失败的可重试错误仅在尚有执行预算时进入 queued。新任务默认最多 1 次，可配置最多 3 次；旧任务预算不变。每次生成内部最多自动修改 3 次。人工审核不通过与后台执行失败是不同状态。

原型 /runs 接口已被带鉴权的 /api/jobs 替代；旧版 CLI 和 RunStore 保留兼容，但不提供多用户功能。线上自动接口文档默认关闭。
