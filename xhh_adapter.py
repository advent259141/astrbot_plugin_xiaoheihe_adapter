from __future__ import annotations

import asyncio
import time
import traceback
from collections import deque
from contextlib import suppress
from typing import Any, cast

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.platform import PlatformStatus

from .xhh_client import (
    IncomingMessage,
    LinkContext,
    XiaoHeiHeClient,
    XiaoHeiHeLoginExpired,
    summarize_link_context_data,
)
from .xhh_event import XiaoHeiHeMessageEvent
from .xhh_session import merge_saved_session_config


XIAOHEIHE_DEFAULT_CONFIG = {
    "type": "xiaoheihe",
    "enable": False,
    "id": "xiaoheihe",
    "cookie": "",
    "use_saved_login": True,
    "heybox_id": "",
    "device_id": "",
    "api_params_url": "",
    "user_agent": "",
    "poll_interval": 60,
    "message_fresh_seconds": 300,
    "listen_mentions": True,
    "listen_comments": True,
    "listen_direct_messages": False,
    "listen_stranger_direct_messages": False,
    "include_link_context": True,
    "max_context_comment_lines": 8,
    "message_limit": 20,
    "direct_message_conversation_limit": 30,
    "max_reply_chars": 800,
    "request_timeout": 20,
    "comment_cooldown_seconds": 30,
    "direct_message_cooldown_seconds": 5,
    "debug_log_raw_messages": False,
}

XIAOHEIHE_CONFIG_METADATA = {
    "cookie": {
        "description": "小黑盒 Cookie",
        "type": "text",
        "hint": "从已登录的小黑盒网页版请求中复制 Cookie。插件不会在状态页回显该字段。",
        "obvious_hint": True,
    },
    "use_saved_login": {
        "description": "使用扫码保存的登录态",
        "type": "bool",
        "hint": "开启后会优先使用插件登录页扫码保存的 Cookie、heybox_id 和 device_id。",
        "default": True,
    },
    "heybox_id": {
        "description": "heybox_id",
        "type": "string",
        "hint": "可选。留空时会尝试从 Cookie 中读取 heybox_id 或 user_heybox_id。",
    },
    "device_id": {
        "description": "device_id",
        "type": "string",
        "hint": "可选。若接口校验设备参数，可从网页版请求参数中复制 device_id。",
    },
    "api_params_url": {
        "description": "真实 API URL",
        "type": "text",
        "hint": (
            "推荐填写。打开开发者工具 Network，复制一条 "
            "https://api.xiaoheihe.cn/... 完整请求 URL；插件会复用其中的 "
            "version、web_version、device_id 等非签名参数。"
        ),
    },
    "user_agent": {
        "description": "User-Agent",
        "type": "string",
        "hint": "可选。默认使用 Chrome 桌面浏览器 User-Agent。",
    },
    "poll_interval": {
        "description": "轮询间隔秒数",
        "type": "int",
        "hint": "建议不要低于 30 秒，避免触发风控。",
        "default": 60,
    },
    "message_fresh_seconds": {
        "description": "新消息时间窗口",
        "type": "int",
        "hint": "只处理该秒数以内的消息。消息没有时间戳时不按过期跳过。",
        "default": 300,
    },
    "listen_mentions": {
        "description": "监听 @ 我的消息",
        "type": "bool",
        "default": True,
    },
    "listen_comments": {
        "description": "监听评论/回复我的消息",
        "type": "bool",
        "default": True,
    },
    "listen_direct_messages": {
        "description": "监听私信",
        "type": "bool",
        "hint": "默认关闭。开启后会读取最近私信会话，并把对方发来的私信转换为 AstrBot 好友消息。",
        "default": False,
    },
    "listen_stranger_direct_messages": {
        "description": "监听陌生人私信",
        "type": "bool",
        "hint": "默认关闭。仅在监听私信开启时生效，会额外读取陌生人私信列表。",
        "default": False,
    },
    "include_link_context": {
        "description": "附带帖子上下文",
        "type": "bool",
        "hint": "处理消息前额外拉取帖子详情和评论区摘要，并追加到 AstrBot 收到的消息文本中。",
        "default": True,
    },
    "max_context_comment_lines": {
        "description": "上下文评论行数",
        "type": "int",
        "hint": "附带帖子上下文时，最多追加多少行评论区摘要。",
        "default": 8,
    },
    "message_limit": {
        "description": "每次拉取消息数",
        "type": "int",
        "default": 20,
    },
    "direct_message_conversation_limit": {
        "description": "每个私信会话拉取条数",
        "type": "int",
        "hint": "监听私信时，每个最近私信会话最多读取多少条历史消息。",
        "default": 30,
    },
    "max_reply_chars": {
        "description": "最大回复字符数",
        "type": "int",
        "hint": "超过后会截断，避免评论过长发送失败。",
        "default": 800,
    },
    "request_timeout": {
        "description": "请求超时秒数",
        "type": "int",
        "default": 20,
    },
    "comment_cooldown_seconds": {
        "description": "评论发送冷却秒数",
        "type": "int",
        "hint": "同一平台实例连续发表评论的最小间隔。",
        "default": 30,
    },
    "direct_message_cooldown_seconds": {
        "description": "私信发送冷却秒数",
        "type": "int",
        "hint": "同一平台实例连续发送私信的最小间隔。",
        "default": 5,
    },
    "debug_log_raw_messages": {
        "description": "调试日志输出原始消息",
        "type": "bool",
        "hint": "仅调试时开启，日志里可能包含小黑盒消息内容。",
        "default": False,
    },
}

XIAOHEIHE_I18N_RESOURCES = {
    "zh-CN": XIAOHEIHE_CONFIG_METADATA,
    "en-US": {
        "cookie": {
            "description": "XiaoHeiHe Cookie",
            "hint": "Copy Cookie from a logged-in XiaoHeiHe web request. It is never shown on the status page.",
        },
        "use_saved_login": {
            "description": "Use saved QR login session",
            "hint": "Use Cookie, heybox_id and device_id saved from the plugin login page.",
        },
        "heybox_id": {
            "description": "heybox_id",
            "hint": "Optional. If empty, the adapter tries heybox_id or user_heybox_id from Cookie.",
        },
        "device_id": {
            "description": "device_id",
            "hint": "Optional. Copy it from XiaoHeiHe web query params if device validation is required.",
        },
        "api_params_url": {
            "description": "Captured API URL",
            "hint": (
                "Recommended. Paste a full api.xiaoheihe.cn request URL from "
                "DevTools Network; non-signature web params will be reused."
            ),
        },
        "user_agent": {
            "description": "User-Agent",
            "hint": "Optional. Defaults to a desktop Chrome User-Agent.",
        },
        "poll_interval": {
            "description": "Polling interval seconds",
            "hint": "Prefer 30 seconds or more to reduce account risk.",
        },
        "message_fresh_seconds": {
            "description": "Fresh message window",
            "hint": "Only messages newer than this many seconds are handled.",
        },
        "listen_mentions": {"description": "Listen to mentions"},
        "listen_comments": {"description": "Listen to comments/replies"},
        "listen_direct_messages": {
            "description": "Listen to direct messages",
            "hint": "Disabled by default. When enabled, recent direct-message conversations are converted to AstrBot friend messages.",
        },
        "listen_stranger_direct_messages": {
            "description": "Listen to stranger direct messages",
            "hint": "Disabled by default. Only works when direct-message listening is enabled.",
        },
        "include_link_context": {
            "description": "Include post context",
            "hint": "Fetch post detail and comment summary before dispatching the AstrBot event.",
        },
        "max_context_comment_lines": {
            "description": "Context comment lines",
            "hint": "Maximum number of comment summary lines appended to the event text.",
        },
        "message_limit": {"description": "Messages fetched per poll"},
        "direct_message_conversation_limit": {
            "description": "Direct-message history fetched per conversation",
            "hint": "Maximum history messages fetched from each recent direct-message conversation.",
        },
        "max_reply_chars": {"description": "Maximum reply characters"},
        "request_timeout": {"description": "Request timeout seconds"},
        "comment_cooldown_seconds": {"description": "Comment cooldown seconds"},
        "direct_message_cooldown_seconds": {"description": "Direct-message cooldown seconds"},
        "debug_log_raw_messages": {"description": "Log raw messages for debugging"},
    },
}


@register_platform_adapter(
    "xiaoheihe",
    "小黑盒平台适配器",
    default_config_tmpl=XIAOHEIHE_DEFAULT_CONFIG.copy(),
    adapter_display_name="小黑盒",
    support_streaming_message=False,
    i18n_resources=XIAOHEIHE_I18N_RESOURCES,
    config_metadata=XIAOHEIHE_CONFIG_METADATA,
)
class XiaoHeiHePlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        merged_config = merge_saved_session_config(
            {**XIAOHEIHE_DEFAULT_CONFIG, **(platform_config or {})},
        )
        super().__init__(merged_config, event_queue)
        self.settings = platform_settings
        self.client = XiaoHeiHeClient(self.config)
        self._shutdown_event = asyncio.Event()
        self._seen_message_ids: deque[str] = deque(maxlen=1000)
        self._seen_message_id_set: set[str] = set()
        self._seen_targets: deque[str] = deque(maxlen=1000)
        self._seen_target_set: set[str] = set()
        self._stats: dict[str, Any] = {
            "poll_count": 0,
            "received_count": 0,
            "committed_count": 0,
            "skipped_duplicate_count": 0,
            "skipped_stale_count": 0,
            "context_fetch_error_count": 0,
            "send_count": 0,
            "last_poll_at": None,
            "last_message_at": None,
            "last_send_at": None,
            "last_error": None,
            "last_error_at": None,
        }

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="xiaoheihe",
            description="小黑盒平台适配器",
            id=cast(str, self.config.get("id") or "xiaoheihe"),
            adapter_display_name="小黑盒",
            support_streaming_message=False,
            support_proactive_message=False,
        )

    async def run(self) -> None:
        if not self.config.get("cookie"):
            self.record_error("小黑盒平台配置缺少 Cookie")
            logger.error("[XiaoHeiHe] Cookie is empty, adapter stopped.")
            return
        if not self.client.get_heybox_id():
            self.record_error("缺少 heybox_id，且无法从 Cookie 读取")
            logger.error("[XiaoHeiHe] heybox_id is empty, adapter stopped.")
            return

        logger.info("[XiaoHeiHe] adapter started: %s", self.meta().id)
        self.status = PlatformStatus.RUNNING
        while not self._shutdown_event.is_set():
            started = time.time()
            try:
                await self._poll_once()
                if self.status == PlatformStatus.ERROR:
                    self.clear_errors()
                self.status = PlatformStatus.RUNNING
            except asyncio.CancelledError:
                raise
            except XiaoHeiHeLoginExpired as exc:
                self._record_runtime_error(f"小黑盒登录态已失效: {exc}")
                logger.error("[XiaoHeiHe] login expired: %s", exc)
                break
            except Exception as exc:
                self._record_runtime_error(str(exc), traceback.format_exc())
                logger.error("[XiaoHeiHe] poll failed: %s", exc, exc_info=True)

            interval = max(5, int(self.config.get("poll_interval") or 60))
            elapsed = time.time() - started
            wait_for = max(1.0, interval - elapsed)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=wait_for)

        await self.client.close()
        self.status = PlatformStatus.STOPPED
        logger.info("[XiaoHeiHe] adapter stopped: %s", self.meta().id)

    async def terminate(self) -> None:
        self._shutdown_event.set()
        await self.client.close()

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        text = XiaoHeiHeMessageEvent._message_chain_to_text(message_chain).strip()
        image_urls = XiaoHeiHeMessageEvent._message_chain_to_image_urls(message_chain)
        max_reply_chars = max(1, int(self.config.get("max_reply_chars") or 800))
        if len(text) > max_reply_chars:
            text = text[:max_reply_chars].rstrip()
        if text or image_urls:
            await self.client.send_text_to_session(
                session.session_id,
                text,
                image_urls=image_urls,
                cooldown_seconds=self._send_cooldown_for_session(session.session_id),
            )
            self._stats["send_count"] += 1
            self._stats["last_send_at"] = int(time.time())
        await super().send_by_session(session, message_chain)

    def get_client(self) -> XiaoHeiHeClient:
        return self.client

    async def _poll_once(self) -> None:
        self._stats["poll_count"] += 1
        self._stats["last_poll_at"] = int(time.time())
        limit = max(1, min(int(self.config.get("message_limit") or 20), 50))

        incoming: list[IncomingMessage] = []
        if bool(self.config.get("listen_mentions", True)):
            messages = await self.client.fetch_mentions(limit=limit)
            if self.config.get("debug_log_raw_messages"):
                logger.debug("[XiaoHeiHe] mention raw messages: %s", messages)
            incoming.extend(
                item
                for item in (
                    self.client.normalize_incoming_message(message, source="mention")
                    for message in messages
                    if isinstance(message, dict)
                )
                if item is not None
            )

        if bool(self.config.get("listen_comments", True)):
            messages = await self.client.fetch_comment_messages(limit=limit)
            if self.config.get("debug_log_raw_messages"):
                logger.debug("[XiaoHeiHe] comment raw messages: %s", messages)
            incoming.extend(
                item
                for item in (
                    self.client.normalize_incoming_message(message, source="comment")
                    for message in messages
                    if isinstance(message, dict)
                )
                if item is not None
            )

        if bool(self.config.get("listen_direct_messages", False)):
            direct_messages = await self.client.fetch_direct_messages_from_recent(
                limit=limit,
                conversation_limit=max(
                    1,
                    min(
                        int(self.config.get("direct_message_conversation_limit") or 30),
                        50,
                    ),
                ),
                include_strangers=bool(
                    self.config.get("listen_stranger_direct_messages", False),
                ),
            )
            if self.config.get("debug_log_raw_messages"):
                logger.debug("[XiaoHeiHe] direct message normalized messages: %s", direct_messages)
            incoming.extend(direct_messages)

        incoming.sort(key=lambda item: item.timestamp)
        self._stats["received_count"] += len(incoming)
        for message in incoming:
            if self._should_skip_message(message):
                continue
            await self._commit_incoming(message)

    def _should_skip_message(self, message: IncomingMessage) -> bool:
        fresh_seconds = max(1, int(self.config.get("message_fresh_seconds") or 300))
        if message.timestamp and int(time.time()) - message.timestamp > fresh_seconds:
            self._stats["skipped_stale_count"] += 1
            self._remember(message.message_id, self._seen_message_ids, self._seen_message_id_set)
            self._remember(message.target_key, self._seen_targets, self._seen_target_set)
            return True
        if message.message_id in self._seen_message_id_set:
            self._stats["skipped_duplicate_count"] += 1
            return True
        if message.target_key in self._seen_target_set:
            self._stats["skipped_duplicate_count"] += 1
            return True
        self._remember(message.message_id, self._seen_message_ids, self._seen_message_id_set)
        self._remember(message.target_key, self._seen_targets, self._seen_target_set)
        return False

    @staticmethod
    def _remember(value: str, queue: deque[str], seen: set[str]) -> None:
        value = str(value or "")
        if not value:
            return
        if len(queue) == queue.maxlen and queue:
            seen.discard(queue[0])
        queue.append(value)
        seen.add(value)

    async def _commit_incoming(self, message: IncomingMessage) -> None:
        context = await self._fetch_context(message)
        abm = self._to_astrbot_message(message, context)
        event = XiaoHeiHeMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            client=self.client,
            max_reply_chars=int(self.config.get("max_reply_chars") or 800),
            comment_cooldown_seconds=self._send_cooldown_for_session(abm.session_id),
            on_sent=self._mark_sent,
        )
        event.set_extra("xiaoheihe_source", message.source)
        if message.session_id.startswith("dm!"):
            event.set_extra("xiaoheihe_dm_user_id", message.sender_id)
        else:
            event.set_extra("xiaoheihe_link_id", message.link_id)
            event.set_extra("xiaoheihe_reply_id", message.reply_id)
            event.set_extra("xiaoheihe_root_id", message.root_id)
            event.set_extra("xiaoheihe_link_title", message.link_title)
        event.set_extra("xiaoheihe_rich_text", self._build_rich_text_extra(message, context))
        event.set_extra("xiaoheihe_image_urls", self._collect_image_urls(message, context))
        self.commit_event(event)
        self._stats["committed_count"] += 1
        self._stats["last_message_at"] = int(time.time())

    def _mark_sent(self) -> None:
        self._stats["send_count"] += 1
        self._stats["last_send_at"] = int(time.time())

    def _send_cooldown_for_session(self, session_id: str) -> int:
        if str(session_id or "").startswith("dm!"):
            return int(self.config.get("direct_message_cooldown_seconds") or 5)
        return int(self.config.get("comment_cooldown_seconds") or 30)

    async def _fetch_context(self, message: IncomingMessage) -> LinkContext:
        if message.session_id.startswith("dm!"):
            return LinkContext()
        if not bool(self.config.get("include_link_context", True)):
            return LinkContext()
        try:
            data = await self.client.fetch_link_context(message.link_id)
            return summarize_link_context_data(
                data,
                max_comment_lines=int(
                    self.config.get("max_context_comment_lines") or 8,
                ),
            )
        except Exception as exc:
            self._stats["context_fetch_error_count"] += 1
            logger.warning(
                "[XiaoHeiHe] failed to fetch link context for %s: %s",
                message.link_id,
                exc,
            )
            return LinkContext()

    def _to_astrbot_message(
        self,
        message: IncomingMessage,
        context: LinkContext | None = None,
    ) -> AstrBotMessage:
        context = context or LinkContext()
        if message.session_id.startswith("dm!"):
            text = message.text or message.notification_text or "[小黑盒私信]"
        else:
            text = message.text or message.notification_text or "[小黑盒消息]"
        if message.link_title:
            text = f"{text}\n\n帖子：{message.link_title}"
        if context.text:
            text = f"{text}\n\n---\n{context.text}"
        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = self.client.get_heybox_id() or self.meta().id
        abm.session_id = message.session_id
        abm.message_id = message.message_id
        abm.sender = MessageMember(
            user_id=message.sender_id or "unknown",
            nickname=message.sender_name,
        )
        abm.message_str = text
        abm.message = [Plain(text=text)]
        for url in self._collect_image_urls(message, context):
            abm.message.append(Image(file=url, url=url))
        abm.timestamp = message.timestamp
        abm.raw_message = message.raw or {}
        return abm

    @staticmethod
    def _collect_image_urls(
        message: IncomingMessage,
        context: LinkContext | None = None,
    ) -> list[str]:
        context = context or LinkContext()
        urls = [
            *message.image_urls,
            *message.replied_image_urls,
            *context.image_urls,
            *context.comment_image_urls,
        ]
        return list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))

    @staticmethod
    def _build_rich_text_extra(
        message: IncomingMessage,
        context: LinkContext | None = None,
    ) -> dict[str, Any]:
        context = context or LinkContext()
        return {
            "message": message.rich_text,
            "context": context.rich_text,
        }

    def _record_runtime_error(
        self,
        message: str,
        traceback_str: str | None = None,
    ) -> None:
        self._stats["last_error"] = message
        self._stats["last_error_at"] = int(time.time())
        self.record_error(message, traceback_str)

    def get_xiaoheihe_stats(self, *, show_sensitive: bool = False) -> dict[str, Any]:
        data = {
            "id": self.meta().id,
            "type": self.meta().name,
            "status": self.status.value,
            "heybox_id": self.client.get_heybox_id() if show_sensitive else "***",
            "device_id": self.config.get("device_id") if show_sensitive else "***",
            "saved_login_applied": bool(self.config.get("_saved_login_applied")),
            "has_api_params_url": bool(self.config.get("api_params_url")),
            "listen_mentions": bool(self.config.get("listen_mentions", True)),
            "listen_comments": bool(self.config.get("listen_comments", True)),
            "listen_direct_messages": bool(
                self.config.get("listen_direct_messages", False),
            ),
            "listen_stranger_direct_messages": bool(
                self.config.get("listen_stranger_direct_messages", False),
            ),
            "poll_interval": int(self.config.get("poll_interval") or 60),
            "message_fresh_seconds": int(
                self.config.get("message_fresh_seconds") or 300,
            ),
            "seen_message_count": len(self._seen_message_id_set),
            **self._stats,
        }
        last_error = self.last_error
        if last_error:
            data["platform_last_error"] = {
                "message": last_error.message,
                "timestamp": last_error.timestamp.isoformat(),
            }
        return data
