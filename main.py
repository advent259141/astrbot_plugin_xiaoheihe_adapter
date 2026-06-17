from __future__ import annotations

from typing import Any

from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_xiaoheihe_adapter"


class XiaoHeiHeAdapterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._login_client = None
        self._login_qr = ""

        # Importing the module registers the platform adapter through the decorator.
        from . import xhh_adapter as _xhh_adapter  # noqa: F401
        from .xhh_client import XiaoHeiHeClient
        from .xhh_tools import build_xiaoheihe_tools

        self._client_cls = XiaoHeiHeClient
        self.context.add_llm_tools(*build_xiaoheihe_tools(self.config))

        context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self.status,
            ["GET"],
            "XiaoHeiHe adapter status",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/page/<page_name>",
            self.page_entry,
            ["GET"],
            "XiaoHeiHe plugin page entry",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/login/start",
            self.login_start,
            ["POST"],
            "Start XiaoHeiHe QR login",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/login/poll",
            self.login_poll,
            ["POST"],
            "Poll XiaoHeiHe QR login",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/login/session",
            self.login_session,
            ["GET"],
            "XiaoHeiHe saved login session",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/login/clear",
            self.login_clear,
            ["POST"],
            "Clear XiaoHeiHe saved login session",
        )

    async def status(self):
        if not bool(self.config.get("status_page_enabled", True)):
            return json_response(
                {
                    "enabled": False,
                    "instances": [],
                    "message": "status page disabled",
                },
            )

        show_sensitive = bool(self.config.get("show_sensitive_status", False))
        instances = []
        for platform in getattr(self.context.platform_manager, "platform_insts", []):
            meta = platform.meta()
            if meta.name != "xiaoheihe":
                continue
            getter = getattr(platform, "get_xiaoheihe_stats", None)
            if callable(getter):
                instances.append(getter(show_sensitive=show_sensitive))
            else:
                instances.append(_fallback_platform_stats(platform))

        return json_response(
            {
                "enabled": True,
                "show_sensitive": show_sensitive,
                "saved_login": _saved_login_status(show_sensitive=show_sensitive),
                "instances": instances,
            },
        )

    async def page_entry(self, page_name: str):
        page_name = str(page_name or "").strip()
        if page_name not in {"login", "status"}:
            return error_response("unknown page", status_code=404)

        try:
            raw_request = request._request
            page_service = raw_request.app.state.services.plugin_pages
            locale = _request_locale()
            return json_response(
                await page_service.get_plugin_page_entry_config(
                    plugin_name=request.plugin_name or PLUGIN_NAME,
                    page_name=page_name,
                    username=request.username,
                    locale=locale,
                ),
            )
        except Exception as exc:
            return error_response(str(exc), status_code=500)

    async def login_start(self):
        await self._close_login_client()
        self._login_client = self._client_cls(
            {
                "request_timeout": int(self.config.get("login_request_timeout") or 20),
                "user_agent": self.config.get("user_agent", ""),
            },
        )
        try:
            data = await self._login_client.request_qr_login()
            from .xhh_qr import make_qr_svg

            data["qr_svg"] = make_qr_svg(str(data.get("qr_url") or ""))
        except Exception as exc:
            await self._close_login_client()
            return error_response(str(exc), status_code=500)
        self._login_qr = str(data.get("qr") or "")
        return json_response(data)

    async def login_poll(self):
        payload = await request.json(default={})
        qr = str(payload.get("qr") or self._login_qr or "").strip()
        if not qr:
            return error_response("missing qr", status_code=400)
        if self._login_client is None:
            self._login_client = self._client_cls(
                {
                    "request_timeout": int(self.config.get("login_request_timeout") or 20),
                    "user_agent": self.config.get("user_agent", ""),
                },
            )
        try:
            data = await self._login_client.poll_qr_login(qr)
        except Exception as exc:
            return error_response(str(exc), status_code=500)
        if data.get("status") == "created":
            from .xhh_session import save_login_session

            saved = save_login_session(data)
            await self._close_login_client()
            return json_response(_public_login_result(saved))
        return json_response(data)

    async def login_session(self):
        show_sensitive = bool(self.config.get("show_sensitive_status", False))
        return json_response(_saved_login_status(show_sensitive=show_sensitive))

    async def login_clear(self):
        from .xhh_session import clear_saved_session

        clear_saved_session()
        await self._close_login_client()
        return json_response({"cleared": True})

    async def terminate(self) -> None:
        await self._close_login_client()
        await super().terminate()

    async def _close_login_client(self) -> None:
        if self._login_client is not None:
            await self._login_client.close()
        self._login_client = None
        self._login_qr = ""


def _fallback_platform_stats(platform: Any) -> dict[str, Any]:
    meta = platform.meta()
    status = getattr(getattr(platform, "status", None), "value", "unknown")
    return {
        "id": meta.id,
        "type": meta.name,
        "status": status,
    }


def _saved_login_status(*, show_sensitive: bool = False) -> dict[str, Any]:
    from .xhh_session import public_session_status

    return public_session_status(show_sensitive=show_sensitive)


def _request_locale(default: str = "zh-CN") -> str:
    raw = str(request.headers.get("Accept-Language", "") or "").strip()
    locale = raw.split(",", 1)[0].split(";", 1)[0].strip()
    return locale if 0 < len(locale) <= 32 else default


def _public_login_result(saved: dict[str, Any]) -> dict[str, Any]:
    public = _saved_login_status(show_sensitive=False)
    return {
        "status": "created",
        "saved": True,
        "session": public,
    }
