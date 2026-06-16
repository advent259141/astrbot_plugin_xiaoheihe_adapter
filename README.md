# 小黑盒 AstrBot 平台适配器

这个插件把小黑盒网页登录态接入 AstrBot：轮询小黑盒通知消息，将 `@我的消息`、评论/回复消息和可选私信转换成 AstrBot 事件，再通过小黑盒评论/私信接口回复。

## 功能

- 接收 `@我的消息`、评论/回复消息。
- 可选接收私信、陌生人私信，并按 AstrBot 好友消息投递。
- 清洗小黑盒富文本中的 HTML、@用户和表情文本。
- 读取帖子正文、评论区和回复中的图片，并作为 AstrBot `Image` 组件传入事件。
- 回复文本评论。
- 回复图片：HTTP/HTTPS 图片会先调用小黑盒转存接口，本地图片文件会按网页端流程直传小黑盒 COS，再按评论图片格式发送。
- 回复私信文本和图片；图片同样支持 HTTP/HTTPS 转存和本地文件直传 COS。

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
- `listen_direct_messages`：默认关闭。开启后轮询最近私信会话，只投递对方发来的消息。
- `listen_stranger_direct_messages`：默认关闭。开启后会额外读取陌生人私信列表，需同时开启 `listen_direct_messages`。
- `direct_message_conversation_limit`：每个最近私信会话拉取的历史条数，默认 30。
- `direct_message_cooldown_seconds`：私信发送冷却秒数，默认 5。

## 原理

小黑盒没有公开 Bot API。插件复用网页登录态：

- `GET /account/get_qrcode_url/` 创建登录二维码。
- `GET /account/qr_state/` 轮询扫码状态并保存登录 Cookie。
- `GET /bbs/app/user/message` 拉取通知消息。
- `GET /bbs/app/user/message?list_type=2` 拉取最近私信会话。
- `GET /chat/stranger_messages/` 拉取陌生人私信会话。
- `GET /chatroom/v2/msg/user` 拉取某个用户的私信历史。
- `GET /bbs/app/link/tree` 获取帖子和评论上下文。
- `GET /bbs/app/api/qcloud/cos/copy/image/by/url` 转存待发送的 HTTP 图片。
- `POST /bbs/app/api/qcloud/cos/upload/info/v2`、`upload/token/v2`、`upload/callback/v2` 申请本地图片上传 key、临时凭证和回调预览地址。
- `PUT https://<bucket>.cos.<region>.myqcloud.com/<key>` 直传本地图片文件到小黑盒 COS。
- `POST /bbs/app/comment/create` 发送评论回复。
- `POST /chatroom/v2/msg/user` 发送私信。
- 消息查询、帖子详情、评论发送等业务 API 会带 `hkey`、`_time`、`nonce` 签名参数；扫码登录的 QR 接口按网页原始请求只带轻量参数。

## 限制

- 图片发送支持 AstrBot `Image` 组件里的 HTTP/HTTPS URL、`file://` URL 和本地图片路径；本地直传目前覆盖 PNG、JPG/JPEG、GIF、WebP、BMP。
- 私信监听默认关闭；开启前请确认你的隐私和自动回复策略。插件会过滤自己发出的私信，避免自触发循环。
- 语音、视频、文件会转成占位文本。
- 这是网页登录态模拟，不是官方 Bot API，账号风控风险需要自行承担。
- 登录态过期后需要重新扫码。
- 小黑盒接口字段或签名规则变化时，需要更新插件。
