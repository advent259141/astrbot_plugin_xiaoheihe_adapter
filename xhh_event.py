from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, AtAll, File, Image, Plain, Record, Video
from astrbot.api.platform import AstrBotMessage, PlatformMetadata

from .xhh_client import XiaoHeiHeClient


class XiaoHeiHeMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: XiaoHeiHeClient,
        *,
        max_reply_chars: int = 800,
        comment_cooldown_seconds: int = 30,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client
        self.max_reply_chars = max(1, int(max_reply_chars or 800))
        self.comment_cooldown_seconds = max(0, int(comment_cooldown_seconds or 0))
        self._on_sent = on_sent

    async def send(self, message: MessageChain) -> None:
        text = self._message_chain_to_text(message).strip()
        if text:
            if len(text) > self.max_reply_chars:
                text = text[: self.max_reply_chars].rstrip()
            await self.client.send_text_to_session(
                self.session_id,
                text,
                cooldown_seconds=self.comment_cooldown_seconds,
            )
            if self._on_sent:
                self._on_sent()
        await super().send(message)

    @staticmethod
    def _message_chain_to_text(message: MessageChain) -> str:
        parts: list[str] = []
        for comp in message.chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, AtAll):
                parts.append("@全体成员")
            elif isinstance(comp, At):
                parts.append(f"@{comp.name or comp.qq}")
            elif isinstance(comp, Image):
                url = _first_text(getattr(comp, "url", ""), getattr(comp, "file", ""))
                parts.append(f"[图片:{url}]" if url.startswith("http") else "[图片]")
            elif isinstance(comp, Record):
                parts.append("[语音]")
            elif isinstance(comp, Video):
                parts.append("[视频]")
            elif isinstance(comp, File):
                name = _first_text(getattr(comp, "name", ""), getattr(comp, "url", ""))
                parts.append(f"[文件:{name}]" if name else "[文件]")
            else:
                parts.append(f"[{getattr(comp, 'type', comp.__class__.__name__)}]")
        return "".join(parts)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
