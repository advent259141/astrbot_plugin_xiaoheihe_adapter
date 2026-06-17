from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .xhh_client import (
    FeedPostSummary,
    XiaoHeiHeClient,
    summarize_link_context_data,
)
from .xhh_session import merge_saved_session_config


PLUGIN_NAME = "astrbot_plugin_xiaoheihe_adapter"


class XiaoHeiHeToolError(RuntimeError):
    """Raised when a XiaoHeiHe tool cannot be executed."""


class XiaoHeiHeToolMixin:
    plugin_config: dict[str, Any]

    @asynccontextmanager
    async def _client(self, context: ContextWrapper[AstrAgentContext]) -> AsyncIterator[XiaoHeiHeClient]:
        ctx = context.context.context
        for platform in getattr(ctx.platform_manager, "platform_insts", []):
            try:
                meta = platform.meta()
            except Exception:
                continue
            if meta.name != "xiaoheihe":
                continue
            getter = getattr(platform, "get_client", None)
            if callable(getter):
                yield getter()
                return

        config = merge_saved_session_config(dict(self.plugin_config or {}))
        if not config.get("cookie"):
            raise XiaoHeiHeToolError("没有可用的小黑盒登录态，请先在插件 login 页面扫码登录，或启动 xiaoheihe 平台实例。")
        client = XiaoHeiHeClient(config)
        try:
            yield client
        finally:
            await client.close()

    def _comment_cooldown(self) -> int:
        return max(0, int(self.plugin_config.get("tool_comment_cooldown_seconds") or 60))

    def _action_guard(self, confirm: Any) -> dict[str, Any] | None:
        if not bool(self.plugin_config.get("enable_llm_action_tools", False)):
            return {
                "ok": False,
                "error": "主动操作工具未启用。请在插件配置中开启 enable_llm_action_tools。",
            }
        if not bool(confirm):
            return {
                "ok": False,
                "error": "缺少用户确认，未执行。请先向用户展示操作目标并获得明确确认。",
            }
        return None


@dataclass
class XiaoHeiHeListFeedsTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_list_feeds"
    description: str = "读取小黑盒首页推荐帖子列表，返回帖子 ID、标题、作者、摘要、点赞数、评论数和图片链接。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回帖子数量，1 到 20，默认 10。",
                    "minimum": 1,
                    "maximum": 20,
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移，默认 0。",
                    "minimum": 0,
                },
            },
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        limit = max(1, min(int(kwargs.get("limit") or 10), 20))
        offset = max(0, int(kwargs.get("offset") or 0))
        async with self._client(context) as client:
            posts = await client.fetch_feed_post_summaries(limit=limit, offset=offset)
        return _json_result(
            {
                "ok": True,
                "count": len(posts),
                "posts": [_feed_post_to_dict(post) for post in posts[:limit]],
            },
        )


@dataclass
class XiaoHeiHeReadPostTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_read_post"
    description: str = "读取指定小黑盒帖子的正文、图片和评论区摘要。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "link_id": {
                    "type": "string",
                    "description": "小黑盒帖子 ID。",
                },
                "max_comment_lines": {
                    "type": "integer",
                    "description": "最多返回多少行评论摘要，默认 12，最大 30。",
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": ["link_id"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        link_id = str(kwargs.get("link_id") or "").strip()
        if not link_id:
            return _json_result({"ok": False, "error": "缺少 link_id"})
        max_comment_lines = max(1, min(int(kwargs.get("max_comment_lines") or 12), 30))
        async with self._client(context) as client:
            data = await client.fetch_link_context(link_id)
        summary = summarize_link_context_data(data, max_comment_lines=max_comment_lines)
        return _json_result(
            {
                "ok": True,
                "link_id": link_id,
                "text": summary.text,
                "image_urls": summary.image_urls,
                "comment_image_urls": summary.comment_image_urls,
                "rich_text": summary.rich_text,
            },
        )


@dataclass
class XiaoHeiHeSearchPostsTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_search_posts"
    description: str = "按关键词搜索小黑盒内容帖，返回帖子 ID、标题、作者、摘要、点赞数、评论数和图片链接。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回帖子数量，1 到 20，默认 10。",
                    "minimum": 1,
                    "maximum": 20,
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移，默认 0。",
                    "minimum": 0,
                },
                "time_range": {
                    "type": "string",
                    "description": "可选时间范围过滤，留空使用默认排序。",
                },
                "filter_tag": {
                    "type": "string",
                    "description": "可选搜索结果标签过滤。",
                },
            },
            "required": ["query"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return _json_result({"ok": False, "error": "缺少 query"})
        limit = max(1, min(int(kwargs.get("limit") or 10), 20))
        offset = max(0, int(kwargs.get("offset") or 0))
        async with self._client(context) as client:
            posts = await client.search_post_summaries(
                query,
                limit=limit,
                offset=offset,
                time_range=str(kwargs.get("time_range") or "").strip(),
                filter_tag=str(kwargs.get("filter_tag") or "").strip(),
            )
        return _json_result(
            {
                "ok": True,
                "query": query,
                "count": len(posts),
                "posts": [_feed_post_to_dict(post) for post in posts[:limit]],
            },
        )


@dataclass
class XiaoHeiHeCommentPostTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_comment_post"
    description: str = "在指定小黑盒帖子下发送主评论。这个工具会代表当前登录账号发言，必须先获得用户确认。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "link_id": {
                    "type": "string",
                    "description": "小黑盒帖子 ID。",
                },
                "text": {
                    "type": "string",
                    "description": "要发送的主评论内容。",
                },
                "image_urls": {
                    "type": "array",
                    "description": "可选图片 URL 或本地图片路径。HTTP 图片会转存，本地图片会上传到小黑盒 COS。",
                    "items": {"type": "string"},
                },
                "confirm": {
                    "type": "boolean",
                    "description": "用户明确确认发送后传 true。未确认时不得发送。",
                },
            },
            "required": ["link_id", "text", "confirm"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        if not bool(self.plugin_config.get("enable_llm_comment_tool", False)):
            return _json_result(
                {
                    "ok": False,
                    "error": "主动评论工具未启用。请在插件配置中开启 enable_llm_comment_tool。",
                },
            )
        if not bool(kwargs.get("confirm")):
            return _json_result(
                {
                    "ok": False,
                    "error": "缺少用户确认，未发送。请先向用户展示评论内容并获得明确确认。",
                },
            )
        link_id = str(kwargs.get("link_id") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        image_urls = kwargs.get("image_urls") or []
        if not isinstance(image_urls, list):
            image_urls = []
        if not link_id:
            return _json_result({"ok": False, "error": "缺少 link_id"})
        if not text and not image_urls:
            return _json_result({"ok": False, "error": "评论内容为空"})

        max_chars = max(1, int(self.plugin_config.get("tool_max_comment_chars") or 500))
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()

        async with self._client(context) as client:
            data = await client.submit_comment(
                link_id,
                "-1",
                "-1",
                text,
                image_urls=image_urls,
                cooldown_seconds=self._comment_cooldown(),
            )
        return _json_result(
            {
                "ok": True,
                "link_id": link_id,
                "commentid": data.get("commentid"),
                "message": "评论已发送",
            },
        )


@dataclass
class XiaoHeiHeFavourPostTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_favour_post"
    description: str = "收藏或取消收藏指定小黑盒帖子。这个工具会修改当前登录账号的收藏状态，必须先获得用户确认。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "link_id": {
                    "type": "string",
                    "description": "小黑盒帖子 ID。",
                },
                "favour": {
                    "type": "boolean",
                    "description": "true 表示收藏，false 表示取消收藏。默认 true。",
                },
                "folder_id": {
                    "type": "string",
                    "description": "可选收藏夹 ID；收藏且不填时会自动使用第一个收藏夹。",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "用户明确确认操作后传 true。未确认时不得执行。",
                },
            },
            "required": ["link_id", "confirm"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        guard = self._action_guard(kwargs.get("confirm"))
        if guard:
            return _json_result(guard)
        link_id = str(kwargs.get("link_id") or "").strip()
        if not link_id:
            return _json_result({"ok": False, "error": "缺少 link_id"})
        favour = bool(kwargs.get("favour", True))
        folder_id = str(kwargs.get("folder_id") or "").strip()
        async with self._client(context) as client:
            if favour and not folder_id:
                folders = await client.fetch_favorite_folders()
                if folders:
                    folder_id = str(folders[0].get("id") or folders[0].get("folder_id") or "").strip()
            data = await client.favour_link(link_id, favour=favour, folder_id=folder_id)
        return _json_result(
            {
                "ok": True,
                "link_id": link_id,
                "favour": favour,
                "folder_id": folder_id,
                "status": data.get("status"),
            },
        )


@dataclass
class XiaoHeiHeAwardPostTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_award_post"
    description: str = "点赞或取消点赞指定小黑盒帖子。这个工具会修改当前登录账号的点赞状态，必须先获得用户确认。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "link_id": {
                    "type": "string",
                    "description": "小黑盒帖子 ID。",
                },
                "award": {
                    "type": "boolean",
                    "description": "true 表示点赞，false 表示取消点赞。默认 true。",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "用户明确确认操作后传 true。未确认时不得执行。",
                },
            },
            "required": ["link_id", "confirm"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        guard = self._action_guard(kwargs.get("confirm"))
        if guard:
            return _json_result(guard)
        link_id = str(kwargs.get("link_id") or "").strip()
        if not link_id:
            return _json_result({"ok": False, "error": "缺少 link_id"})
        award = bool(kwargs.get("award", True))
        async with self._client(context) as client:
            data = await client.award_link(link_id, award=award)
        return _json_result(
            {
                "ok": True,
                "link_id": link_id,
                "award": award,
                "status": data.get("status"),
            },
        )


@dataclass
class XiaoHeiHeSupportCommentTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_support_comment"
    description: str = "点赞或取消点赞指定小黑盒评论。这个工具会修改当前登录账号的点赞状态，必须先获得用户确认。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "comment_id": {
                    "type": "string",
                    "description": "小黑盒评论 ID。",
                },
                "support": {
                    "type": "boolean",
                    "description": "true 表示点赞，false 表示取消点赞。默认 true。",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "用户明确确认操作后传 true。未确认时不得执行。",
                },
            },
            "required": ["comment_id", "confirm"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        guard = self._action_guard(kwargs.get("confirm"))
        if guard:
            return _json_result(guard)
        comment_id = str(kwargs.get("comment_id") or "").strip()
        if not comment_id:
            return _json_result({"ok": False, "error": "缺少 comment_id"})
        support = bool(kwargs.get("support", True))
        async with self._client(context) as client:
            data = await client.support_comment(comment_id, support=support)
        return _json_result(
            {
                "ok": True,
                "comment_id": comment_id,
                "support": support,
                "status": data.get("status"),
            },
        )


@dataclass
class XiaoHeiHeFollowUserTool(XiaoHeiHeToolMixin, FunctionTool[AstrAgentContext]):
    plugin_config: dict[str, Any] = Field(default_factory=dict)
    name: str = "xiaoheihe_follow_user"
    description: str = "关注或取消关注指定小黑盒用户。这个工具会修改当前登录账号的关注状态，必须先获得用户确认。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "following_id": {
                    "type": "string",
                    "description": "要关注或取关的小黑盒用户 ID。",
                },
                "follow": {
                    "type": "boolean",
                    "description": "true 表示关注，false 表示取消关注。默认 true。",
                },
                "link_id": {
                    "type": "string",
                    "description": "可选帖子 ID；从帖子作者按钮触发时网页端会带该字段。",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "用户明确确认操作后传 true。未确认时不得执行。",
                },
            },
            "required": ["following_id", "confirm"],
        },
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        guard = self._action_guard(kwargs.get("confirm"))
        if guard:
            return _json_result(guard)
        following_id = str(kwargs.get("following_id") or "").strip()
        if not following_id:
            return _json_result({"ok": False, "error": "缺少 following_id"})
        follow = bool(kwargs.get("follow", True))
        async with self._client(context) as client:
            data = await client.follow_user(
                following_id,
                follow=follow,
                link_id=str(kwargs.get("link_id") or "").strip(),
            )
        return _json_result(
            {
                "ok": True,
                "following_id": following_id,
                "follow": follow,
                "status": data.get("status"),
            },
        )


def build_xiaoheihe_tools(config: dict[str, Any]) -> list[FunctionTool[AstrAgentContext]]:
    if not bool(config.get("enable_llm_browse_tools", True)):
        return []
    tools: list[FunctionTool[AstrAgentContext]] = [
        XiaoHeiHeListFeedsTool(plugin_config=config),
        XiaoHeiHeReadPostTool(plugin_config=config),
        XiaoHeiHeSearchPostsTool(plugin_config=config),
    ]
    if bool(config.get("register_llm_comment_tool", True)):
        tools.append(XiaoHeiHeCommentPostTool(plugin_config=config))
    if bool(config.get("register_llm_action_tools", True)):
        tools.extend(
            [
                XiaoHeiHeFavourPostTool(plugin_config=config),
                XiaoHeiHeAwardPostTool(plugin_config=config),
                XiaoHeiHeSupportCommentTool(plugin_config=config),
                XiaoHeiHeFollowUserTool(plugin_config=config),
            ],
        )
    return tools


def _feed_post_to_dict(post: FeedPostSummary) -> dict[str, Any]:
    return {
        "link_id": post.link_id,
        "title": post.title,
        "description": post.description[:500],
        "author_id": post.author_id,
        "author_name": post.author_name,
        "created_at": post.created_at,
        "up": post.up,
        "comment_num": post.comment_num,
        "hashtags": post.hashtags,
        "image_urls": post.image_urls,
        "url": f"https://www.xiaoheihe.cn/app/bbs/link/{post.link_id}",
    }


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
