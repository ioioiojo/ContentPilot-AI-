# 可选服务协议（0.7.0，素材/发布沿用 0.4.0 接口）

关键词搜索服务见 [REFERENCE_SEARCH.md](REFERENCE_SEARCH.md)；前端服务管理、实时热点、自动采集、审稿和加密备份见 [FEATURES.md](FEATURES.md)。当前缺少搜索/媒体/发布/指标服务凭据，模拟验证不等于已接通。

文字接口继续使用 `CONTENTPILOT_PROVIDER=compatible` 和 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。密钥只写服务器环境配置，不写页面、任务内容或导出文件。修改后重启 web 和 worker；Docker 环境重新创建容器。`/api/capabilities` 只表示配置齐全，不能代替供应商连通性验证。

## MCP 与业务技能

`CONTENTPILOT_TOOLS=mcp` 是环境默认值。worker 使用 MCP ClientSession 初始化 stdio 服务并调用 `collect_content_context`，20 秒超时；`local` 模式可直接调用同一工具。服务仅提供本地示例话题及规则，不自动联网抓取热点。任务保存技能 id、版本、指令及工具模式；旧任务没有该字段时保持 local。

## 图片和分镜

分镜复用文字模型，仅生成图片提示词和 shots JSON，不生成视频文件。图片需要 `CONTENTPILOT_IMAGE_MODEL`。默认复用 `OPENAI_BASE_URL` 与 key；设置不同的 `CONTENTPILOT_IMAGE_BASE_URL` 时必须同时提供 `CONTENTPILOT_IMAGE_API_KEY`，避免向不同供应商发送文字服务凭据。

图片调用 OpenAI SDK 的 `images.generate`，要求服务支持 model、prompt、n=1、size=1024x1024、output_format=png，响应 `data[0].b64_json` 为 PNG。接口支持并不意味着指定的文字模型也支持图片。无需配置时保持空白；本项目不预选收费图片模型。

## 视频网关

配置 `CONTENTPILOT_VIDEO_URL` 为可信 HTTPS 任务入口及 `CONTENTPILOT_VIDEO_TOKEN`。这里是项目约定的第三方协议，不是任意视频供应商都能直接接入。OpenAI 原 Videos API 已停用，本项目不把旧 Sora 接口当成当前能力。

POST 入口，Authorization: Bearer TOKEN，Idempotency-Key: 本地素材 id：
~~~json
{"idempotency_key":"素材id","prompt":"事实、已审核文案和素材要求的JSON字符串","content":{},"source":{}}
~~~
响应 `{"id":"远端任务id","status":"queued"}` 或 running。每 3 秒 GET `入口/URL编码后的远端任务id`，直到 `{"id":"远端任务id","status":"completed","b64_json":"MP4的base64"}`；其它最终状态视为失败。远端任务 id 会保存用于人工核对。

素材以数据库 base64 保存，解码后最大 16 MiB，只检查 PNG/MP4 文件签名；正式大规模素材服务需要对象存储与更完整的文件验证。图片 HTTP 超时90秒、视频每次30秒；素材进程总时限由 `JOB_TIMEOUT_SECONDS` 控制（默认180秒，最高1800秒）。失败、超时或租约失效均不自动重发；可能已经计费，重建任务前先核对供应商。素材必须人工检查，可下载或在发布弹窗选择后发送给支持附件的网关。

## 授权发布网关

分别配置 `CONTENTPILOT_PUBLISH_XIAOHONGSHU_URL/_TOKEN`、`CONTENTPILOT_PUBLISH_DOUYIN_URL/_TOKEN`、`CONTENTPILOT_PUBLISH_WECHAT_URL/_TOKEN`。URL 必须 HTTPS；该网关须自行实现平台正式授权及真实发布。项目不提供平台账号、不模拟登录、不使用猜测的官方接口。

仅当前人工通过版本允许提交。POST，Authorization: Bearer TOKEN，Idempotency-Key: 发布记录 id：
~~~json
{"idempotency_key":"发布记录id","platform":"xiaohongshu","content":{"title":"标题","body":"正文","hashtags":[],"call_to_action":"行动建议","platform":"xiaohongshu"},"source":{},"revision":2}
~~~
只有 HTTP 成功并返回 `{"status":"published","url":"https://实际平台内容链接"}` 才记为 published。queued/accepted、超时、错误或无效链接均不能证实实际发布，记录为 unknown；同一 request_key 不重复发送。网关应持久化幂等键，禁止一次请求发布多份内容。HTTP 超时20秒，不跟随重定向，不自动重试。若 web 在外部发送期间中断，记录可能停留 sending，也应先核对网关。

手动模式只保存实际发布链接，不调用外部服务。相同 request_key 须对应相同内容、版本、分组及手动链接；并发冲突返回409。网关结果不明时先核对平台；确认已发布后可通过手动模式登记实际链接，禁止未经核对重新提交网关。

0.7.0 发布可附带明确选择的当前文案/版本图片或视频。网关须支持新增 assets 数组（id、mime、b64_json），不选素材时保持原协议；实际素材上传和平台发布仍由已授权网关执行。

## 效果与质量口径

效果可由用户录入，或通过已配置运营服务定时采集（见 FEATURES.md）。完整指标要求同一口径成交≤点击≤曝光、非负营收与总成本、截至时间和依据；缺失指标保留为空，不用公开互动数推算转化。对每条发布只采用截至时间最新的累计快照；补录旧数据不会覆盖新数据，也不会重复累计。

CTR=点击/曝光；点击成交率=成交/点击；ROI=(营收−录入总成本)/总成本；ROAS=营收/总成本。分母为0返回null。成本须包含投放、素材、模型等所需项目，ROI不是净利润。页面覆盖最近1000条发布；反馈在最近200条发布中选同账号同商品的最多10条有曝光样本。不同平台不合并成同一版本对比；观察数据不能证明因果或随机A/B增益。

人工标注每项1–5分，事实/平台/实用三项均≥4且无事实问题为通过；每个任务/版本/内容只统计最新标注。JSONL导出最多5000条，包含内容快照与标注历史，不含凭据。自动评测结果另列客观规则通过率和模型通过率，不能替代人工事实核查。
