# 小黑盒 AstrBot 平台适配器

这个插件把小黑盒网页登录态接入 AstrBot：轮询小黑盒通知消息，将 `@我的消息`、评论/回复消息转换成 AstrBot 事件，再通过小黑盒评论接口回复。

## 登录

推荐使用插件 Page 扫码登录：

1. 启动或重载 AstrBot。
2. 在插件详情中打开 `login` 页面。
3. 点击“开始扫码”，用小黑盒 App 扫码并在手机端确认。
4. 登录成功后，插件会把 Cookie、heybox_id、device_id 保存到 `data/plugin_data/astrbot_plugin_xiaoheihe_adapter/session.json`。
5. 在平台配置中新增或重载 `xiaoheihe`，保持 `使用扫码保存的登录态` 开启即可。

也可以继续手动填写 Cookie、heybox_id、device_id 和真实 API URL。平台配置里的显式字段优先级会被扫码保存的登录态覆盖，除非关闭 `使用扫码保存的登录态`。

## 平台配置

新增平台时选择 `xiaoheihe`：

- `cookie`：可留空，扫码登录后自动读取保存的 Cookie。
- `use_saved_login`：默认开启，使用插件登录页保存的登录态。
- `heybox_id`：可留空，优先从扫码会话或 Cookie 中读取。
- `api_params_url`：可选。接口校验严格时，可以复制一条 `https://api.xiaoheihe.cn/...` 完整请求 URL。
- `poll_interval`：建议 30 秒以上，降低风控风险。

## 原理

小黑盒没有公开 Bot API。插件复用网页登录态：

- `GET /account/get_qrcode_url/` 创建登录二维码。
- `GET /account/qr_state/` 轮询扫码状态并保存登录 Cookie。
- `GET /bbs/app/user/message` 拉取通知消息。
- `GET /bbs/app/link/tree` 获取帖子和评论上下文。
- `POST /bbs/app/comment/create` 发送评论回复。
- 每个 API 请求都带 `hkey`、`_time`、`nonce` 签名参数。

## 限制

- 只支持文本评论回复；图片、语音、文件会转成占位文本。
- 这是网页登录态模拟，不是官方 Bot API，账号风控风险需要自行承担。
- 登录态过期后需要重新扫码。
- 小黑盒接口字段或签名规则变化时，需要更新插件。
