from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

import aiohttp


API_ORIGIN = "https://api.xiaoheihe.cn"
WEB_ORIGIN = "https://www.xiaoheihe.cn"
MESSAGE_API_PATH = "/bbs/app/user/message"
LINK_TREE_API_PATH = "/bbs/app/link/tree"
COMMENT_CREATE_API_PATH = "/bbs/app/comment/create"
QRCODE_URL_API_PATH = "/account/get_qrcode_url/"
QR_STATE_API_PATH = "/account/qr_state/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

ALLOWED_API_PARAM_KEYS = {
    "os_type",
    "app",
    "client_type",
    "version",
    "web_version",
    "x_client_type",
    "x_app",
    "heybox_id",
    "x_os_type",
    "device_info",
    "device_id",
}

SIGNATURE_PARAM_KEYS = {"hkey", "_time", "nonce"}
LOGIN_COOKIE_NAMES = {
    "user_pkey",
    "user_heybox_id",
    "heybox_id",
    "avatar",
    "level",
    "nickname",
    "x_xhh_tokenid",
}


class XiaoHeiHeClientError(RuntimeError):
    """Raised when XiaoHeiHe API requests fail."""


class XiaoHeiHeLoginExpired(XiaoHeiHeClientError):
    """Raised when the stored web login state is no longer valid."""


@dataclass
class ReplyTarget:
    link_id: str
    reply_id: str
    root_id: str
    sender_id: str = ""
    message_id: str = ""
    source: str = ""

    def to_session_id(self) -> str:
        parts = [
            "reply",
            _escape_session_part(self.link_id),
            _escape_session_part(self.reply_id),
            _escape_session_part(self.root_id),
            _escape_session_part(self.sender_id),
        ]
        return "!".join(parts)

    @classmethod
    def from_session_id(cls, session_id: str) -> "ReplyTarget":
        parts = str(session_id or "").split("!", 4)
        if len(parts) < 5 or parts[0] != "reply":
            raise ValueError(f"unsupported XiaoHeiHe session id: {session_id!r}")
        return cls(
            link_id=_unescape_session_part(parts[1]),
            reply_id=_unescape_session_part(parts[2]),
            root_id=_unescape_session_part(parts[3]) or _unescape_session_part(parts[2]),
            sender_id=_unescape_session_part(parts[4]),
        )


@dataclass
class IncomingMessage:
    message_id: str
    source: str
    text: str
    sender_id: str
    sender_name: str
    timestamp: int
    link_id: str
    reply_id: str
    root_id: str
    session_id: str
    link_title: str = ""
    notification_text: str = ""
    replied_text: str = ""
    raw: dict[str, Any] | None = None

    @property
    def target_key(self) -> str:
        return f"{self.link_id}::{self.reply_id}"


def _escape_session_part(value: str) -> str:
    return str(value or "").replace("%", "%25").replace("!", "%21")


def _unescape_session_part(value: str) -> str:
    return str(value or "").replace("%21", "!").replace("%25", "%")


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _mix_columns(values: list[int]) -> list[int]:
    values = list(values)

    def xtime(value: int) -> int:
        return ((value << 1) ^ 27) & 255 if value & 128 else value << 1

    def q(value: int) -> int:
        return xtime(value) ^ value

    def r(value: int) -> int:
        return q(xtime(value))

    def y(value: int) -> int:
        return r(q(xtime(value)))

    def g(value: int) -> int:
        return y(value) ^ r(value) ^ q(value)

    result = [0, 0, 0, 0]
    result[0] = g(values[0]) ^ y(values[1]) ^ r(values[2]) ^ q(values[3])
    result[1] = q(values[0]) ^ g(values[1]) ^ y(values[2]) ^ r(values[3])
    result[2] = r(values[0]) ^ q(values[1]) ^ g(values[2]) ^ y(values[3])
    result[3] = y(values[0]) ^ r(values[1]) ^ q(values[2]) ^ g(values[3])
    values[:4] = result
    return values


def _map_by_alphabet(value: str, alphabet: str, end: int) -> str:
    source = alphabet[:end]
    return "".join(source[ord(char) % len(source)] for char in value)


def _path_to_alphabet(value: str, alphabet: str) -> str:
    return "".join(alphabet[ord(char) % len(alphabet)] for char in value)


def _interleave(values: Iterable[str]) -> str:
    items = list(values)
    max_length = max((len(value) for value in items), default=0)
    result = []
    for i in range(max_length):
        for value in items:
            if i < len(value):
                result.append(value[i])
    return "".join(result)


def create_signed_params(path: str) -> dict[str, str | int]:
    now = int(time.time())
    nonce = _md5(f"{now}{random.random()}").upper()
    normalized_path = "/" + "/".join(filter(None, path.split("/"))) + "/"
    alphabet = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
    seed = _interleave(
        [
            _map_by_alphabet(str(now + 1), alphabet, -2),
            _path_to_alphabet(normalized_path, alphabet),
            _path_to_alphabet(nonce, alphabet),
        ],
    )[:20]
    hash_value = _md5(seed)
    checksum_values = [ord(char) for char in hash_value[-6:]]
    checksum = str(sum(_mix_columns(checksum_values)) % 100).zfill(2)
    return {
        "hkey": f"{_map_by_alphabet(hash_value[:5], alphabet, -4)}{checksum}",
        "_time": now,
        "nonce": nonce,
    }


def _join_text_parts(parts: Iterable[str]) -> str:
    return "".join(part for part in parts if part)


def _parse_rich_text_json(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_rich_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        parsed = _parse_rich_text_json(value)
        if parsed is not None:
            extracted = _extract_rich_text(parsed)
            if extracted:
                return extracted
        return value

    if isinstance(value, list):
        return _join_text_parts(_extract_rich_text(item) for item in value)

    if isinstance(value, dict):
        for key in ("text", "content", "value", "description", "desc"):
            if key in value:
                text = _extract_rich_text(value.get(key))
                if text:
                    return text

        user = value.get("user") or value.get("target_user") or value.get("mention_user")
        user_name = get_user_display_name(user)
        if user_name:
            return user_name if user_name.startswith("@") else f"@{user_name}"

        for key in ("children", "items", "segments", "spans", "blocks"):
            if key in value:
                text = _extract_rich_text(value.get(key))
                if text:
                    return text

        return ""

    return str(value)


def strip_html(value: Any) -> str:
    text = _extract_rich_text(value)
    text = text.replace("\\u003c", "<").replace("\\u003C", "<")
    text = text.replace("\\u003e", ">").replace("\\u003E", ">")
    text = text.replace("\\u0026", "&")
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\[cube_([^\]]+)\]", r"[\1]", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_cookie_value(cookie_header: str, name: str) -> str:
    parsed = SimpleCookie()
    try:
        parsed.load(str(cookie_header or ""))
    except Exception:
        return ""
    morsel = parsed.get(name)
    return morsel.value.strip() if morsel else ""


def cookie_header_from_values(values: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={value}"
        for name, value in values.items()
        if _string(name) and _string(value)
    )


def merge_cookie_headers(*headers: str) -> str:
    values: dict[str, str] = {}
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(str(header or ""))
        except Exception:
            continue
        for name, morsel in parsed.items():
            value = _string(morsel.value)
            if value:
                values[name] = value
    return cookie_header_from_values(values)


def random_device_id() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def normalize_api_params(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        source = value.get("params") if isinstance(value.get("params"), dict) else value
        return {
            key: _string(source.get(key))
            for key in ALLOWED_API_PARAM_KEYS
            if _string(source.get(key))
        }

    text = _string(value)
    if not text:
        return {}

    if text.startswith("{"):
        try:
            return normalize_api_params(json.loads(text))
        except Exception:
            return {}

    parsed = urlparse(text)
    query_text = parsed.query if parsed.query else text.lstrip("?")
    pairs = parse_qsl(query_text, keep_blank_values=False)
    ret: dict[str, str] = {}
    for key, raw_value in pairs:
        if key in ALLOWED_API_PARAM_KEYS and key not in SIGNATURE_PARAM_KEYS:
            value_text = _string(raw_value)
            if value_text:
                ret[key] = value_text
    return ret


def get_user_id(user: Any) -> str:
    user = _as_dict(user)
    return _string(
        user.get("heybox_id")
        or user.get("user_heybox_id")
        or user.get("userid")
        or user.get("user_id")
        or user.get("uid")
        or user.get("id"),
    )


def get_user_display_name(user: Any) -> str:
    user = _as_dict(user)
    return _string(user.get("username") or user.get("nickname") or user.get("name"))


def get_link_id_from_message(message: dict[str, Any]) -> str:
    link = _as_dict(message.get("link"))
    target = _as_dict(message.get("target"))
    return _string(
        link.get("linkid")
        or link.get("link_id")
        or message.get("linkid")
        or message.get("link_id")
        or target.get("linkid")
        or target.get("link_id"),
    )


def find_first_field_deep(
    source: Any,
    names: set[str],
    seen: set[int] | None = None,
) -> Any:
    if seen is None:
        seen = set()
    if not isinstance(source, dict | list):
        return ""
    source_id = id(source)
    if source_id in seen:
        return ""
    seen.add(source_id)

    if isinstance(source, dict):
        for name in names:
            value = source.get(name)
            if value not in (None, ""):
                return value
        iterable = source.values()
    else:
        iterable = source

    for value in iterable:
        found = find_first_field_deep(value, names, seen)
        if found not in (None, ""):
            return found
    return ""


def get_reply_comment_id_from_message(message: dict[str, Any]) -> str:
    return _string(
        find_first_field_deep(
            message,
            {
                "comment_id",
                "commentid",
                "commentId",
                "comment_a_id",
                "replyid",
                "reply_id",
                "cid",
            },
        ),
    )


def get_root_comment_id_from_message(message: dict[str, Any]) -> str:
    return _string(
        find_first_field_deep(
            message,
            {"root_id", "root_comment_id", "rootCommentId", "root_commentid"},
        ),
    )


def get_message_timestamp(message: dict[str, Any]) -> int:
    value = _number(message.get("timestamp") or message.get("time"))
    if value <= 0:
        return int(time.time())
    return int(value / 1000 if value > 100_000_000_000 else value)


def get_message_text(message: dict[str, Any]) -> str:
    return strip_html(
        message.get("comment_a_text")
        or message.get("comment_text")
        or message.get("content")
        or message.get("text")
        or "",
    )


def get_notification_text(message: dict[str, Any]) -> str:
    return strip_html(message.get("text") or "")


def get_replied_text(message: dict[str, Any]) -> str:
    return strip_html(message.get("comment_b_text") or "")


def get_link_title(message: dict[str, Any]) -> str:
    link = _as_dict(message.get("link"))
    return _string(link.get("title"))


def get_comment_id(comment: Any) -> str:
    comment = _as_dict(comment)
    return _string(
        comment.get("comment_id")
        or comment.get("commentid")
        or comment.get("commentId")
        or comment.get("id")
        or comment.get("cid"),
    )


def normalize_comment_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = _as_dict(data.get("result"))
    raw_comments = (
        result.get("comments")
        or result.get("comment")
        or data.get("comments")
        or []
    )
    groups: list[dict[str, Any]] = []
    for item in _as_list(raw_comments):
        if not isinstance(item, dict):
            continue
        if item.get("root") or item.get("comment"):
            root_source = item.get("root") or item.get("comment")
            root = _as_list(root_source)[0] if isinstance(root_source, list) and root_source else root_source
            replies = (
                item.get("replies")
                or item.get("children")
                or item.get("sub_comments")
                or item.get("subComments")
                or []
            )
            if root:
                groups.append({"root": root, "replies": _as_list(replies)})
            continue
        replies = item.get("replies") or item.get("children") or []
        groups.append({"root": item, "replies": _as_list(replies)})
    return groups


def get_comment_line(comment: Any) -> str:
    comment = _as_dict(comment)
    user = _as_dict(comment.get("user"))
    user_name = get_user_display_name(user) or "匿名用户"
    text = strip_html(comment.get("text") or comment.get("content") or "")
    return f"{user_name}: {text}" if text else ""


def summarize_link_context(
    data: dict[str, Any] | None,
    *,
    max_comment_lines: int = 8,
) -> str:
    if not data:
        return ""
    result = _as_dict(data.get("result"))
    link = _as_dict(result.get("link"))

    parts: list[str] = []
    title = _string(link.get("title"))
    content = strip_html(link.get("text") or link.get("description") or "")
    if title:
        parts.append(f"帖子标题：{title}")
    if content:
        parts.append(f"帖子内容：{content[:600]}")

    lines: list[str] = []
    for group in normalize_comment_groups(data):
        comments = [_as_dict(group.get("root")), *_as_list(group.get("replies"))]
        for comment in comments:
            line = get_comment_line(comment)
            if line:
                lines.append(line)

    if lines:
        selected = lines[: max(1, int(max_comment_lines or 8))]
        parts.append("评论区摘要：\n" + "\n".join(selected))
    return "\n\n".join(parts)


def is_login_expired_response(data: Any) -> bool:
    data = _as_dict(data)
    status = _string(data.get("status"))
    text = " ".join(
        _string(data.get(key)) for key in ("status", "msg", "message", "error")
    )
    return status in {"unauthorized", "login_required"} or bool(
        re.search(r"登录|login|unauthorized|401", text, flags=re.I),
    )


def api_error_message(data: Any, fallback: str) -> str:
    data = _as_dict(data)
    parts = [
        data.get("message"),
        data.get("msg"),
        data.get("error"),
        f"status={data.get('status')}" if data.get("status") != "ok" else "",
        fallback,
    ]
    return "；".join(_string(part) for part in parts if _string(part)) or fallback


class XiaoHeiHeClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.cookie = _string(config.get("cookie"))
        self.api_params = normalize_api_params(
            config.get("api_params")
            or config.get("api_params_url")
            or config.get("captured_api_url"),
        )
        self.heybox_id = (
            _string(config.get("heybox_id"))
            or parse_cookie_value(self.cookie, "heybox_id")
            or parse_cookie_value(self.cookie, "user_heybox_id")
            or _string(self.api_params.get("heybox_id"))
        )
        self.device_id = _string(config.get("device_id")) or _string(
            self.api_params.get("device_id"),
        ) or random_device_id()
        self.user_agent = _string(config.get("user_agent")) or DEFAULT_USER_AGENT
        self.timeout = max(3, int(config.get("request_timeout") or 20))
        self._session: aiohttp.ClientSession | None = None
        self._send_lock = asyncio.Lock()
        self._last_comment_sent_at = 0.0
        if self.device_id:
            self.api_params.setdefault("device_id", self.device_id)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def get_heybox_id(self) -> str:
        return self.heybox_id

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "zh,zh-CN;q=0.9",
            "User-Agent": self.user_agent,
            "Referer": f"{WEB_ORIGIN}/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        self._session = aiohttp.ClientSession(timeout=timeout, headers=self._base_headers())
        return self._session

    def build_api_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        query_params: dict[str, Any] = {
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "x_os_type": "Windows",
            "device_info": "Chrome",
        }
        query_params.update(self.api_params)
        if self.device_id:
            query_params["device_id"] = self.device_id
        if params:
            query_params.update(
                {key: value for key, value in params.items() if value is not None},
            )
        query_params.update(create_signed_params(path))
        return f"{API_ORIGIN}{path}?{urlencode(query_params)}"

    def build_login_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        query_params: dict[str, Any] = {}
        if params:
            query_params.update(
                {key: value for key, value in params.items() if value is not None},
            )
        if not query_params:
            return f"{API_ORIGIN}{path}"
        return f"{API_ORIGIN}{path}?{urlencode(query_params)}"

    async def _request_json(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        session = await self._get_session()
        async with session.request(method, url, **kwargs) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                text = await response.text()
                raise XiaoHeiHeClientError(
                    f"小黑盒接口返回非 JSON: HTTP {response.status}, {text[:200]}",
                ) from exc
            if response.status >= 400:
                if is_login_expired_response(data):
                    raise XiaoHeiHeLoginExpired(api_error_message(data, "登录态已失效"))
                raise XiaoHeiHeClientError(f"小黑盒接口请求失败: HTTP {response.status}")
            if is_login_expired_response(data):
                raise XiaoHeiHeLoginExpired(api_error_message(data, "登录态已失效"))
            return data

    async def request_qr_login(self) -> dict[str, Any]:
        url = self.build_login_url(
            QRCODE_URL_API_PATH,
            {
                "app": "web",
                "_notip": "true",
            },
        )
        data = await self._request_json("GET", url)
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "QR code request failed"))
        result = _as_dict(data.get("result"))
        qr_url = _string(result.get("qr_url"))
        if not qr_url:
            raise XiaoHeiHeClientError("QR code response has no qr_url")
        qr_id = _string(dict(parse_qsl(urlparse(qr_url).query)).get("qr"))
        if not qr_id:
            raise XiaoHeiHeClientError("QR code response has no qr id")
        return {
            "status": "pending",
            "qr": qr_id,
            "qr_url": qr_url,
            "expire": int(_number(result.get("expire")) or 120),
            "interval": 1500,
            "device_id": self.device_id,
        }

    async def poll_qr_login(self, qr: str) -> dict[str, Any]:
        qr = _string(qr)
        if not qr:
            raise XiaoHeiHeClientError("missing qr id")
        url = self.build_login_url(
            QR_STATE_API_PATH,
            {
                "qr": qr,
                "app": "web",
            },
        )
        session = await self._get_session()
        async with session.get(url) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                text = await response.text()
                raise XiaoHeiHeClientError(
                    f"QR state response is not JSON: HTTP {response.status}, {text[:200]}",
                ) from exc
            if response.status >= 400:
                raise XiaoHeiHeClientError(f"QR state request failed: HTTP {response.status}")
            set_cookie = response.headers.getall("Set-Cookie", [])

        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "QR state request failed"))

        result = _as_dict(data.get("result"))
        raw_state = _string(result.get("error")) or "wait"
        if raw_state in {"wait", "ready"}:
            return {
                "status": "pending",
                "qr_status": raw_state,
                "message": _string(result.get("error_msg")),
            }
        if raw_state in {"cancel", "canceled", "denied"}:
            return {
                "status": "denied",
                "qr_status": raw_state,
                "message": _string(result.get("error_msg")) or "login cancelled",
            }
        if raw_state in {"timeout", "expired"}:
            return {
                "status": "expired",
                "qr_status": raw_state,
                "message": _string(result.get("error_msg")) or "QR code expired",
            }
        if raw_state != "ok":
            return {
                "status": "error",
                "qr_status": raw_state,
                "message": _string(result.get("error_msg")) or "QR login failed",
            }

        cookie = self._collect_login_cookie(set_cookie)
        heybox_id = (
            _string(result.get("heyboxid"))
            or parse_cookie_value(cookie, "heybox_id")
            or parse_cookie_value(cookie, "user_heybox_id")
        )
        if not cookie:
            raise XiaoHeiHeClientError("QR login succeeded but no login cookie was captured")
        if not heybox_id:
            raise XiaoHeiHeClientError("QR login succeeded but no heybox_id was captured")

        account_detail = _as_dict(result.get("account_detail"))
        level_info = _as_dict(account_detail.get("level_info"))
        saved_api_params = dict(self.api_params)
        saved_api_params.update(
            {
                "os_type": "web",
                "app": "heybox",
                "client_type": "web",
                "version": saved_api_params.get("version") or "999.0.4",
                "web_version": saved_api_params.get("web_version") or "2.5",
                "x_client_type": "web",
                "x_app": "heybox_website",
                "x_os_type": saved_api_params.get("x_os_type") or "Windows",
                "device_info": saved_api_params.get("device_info") or "Chrome",
                "device_id": self.device_id,
                "heybox_id": heybox_id,
            },
        )

        self.cookie = cookie
        self.heybox_id = heybox_id
        self.api_params = normalize_api_params(saved_api_params)
        return {
            "status": "created",
            "qr_status": raw_state,
            "cookie": cookie,
            "heybox_id": heybox_id,
            "device_id": self.device_id,
            "user_agent": self.user_agent,
            "api_params": saved_api_params,
            "nickname": _string(result.get("nickname")),
            "avatar": _string(result.get("avatar")),
            "level": _string(level_info.get("level")),
        }

    def _collect_login_cookie(self, set_cookie_headers: list[str]) -> str:
        values: dict[str, str] = {}
        for header in set_cookie_headers:
            parsed = SimpleCookie()
            try:
                parsed.load(header)
            except Exception:
                continue
            for name, morsel in parsed.items():
                if name in LOGIN_COOKIE_NAMES and _string(morsel.value):
                    values[name] = morsel.value

        if self._session is not None:
            try:
                values.update(
                    {
                        cookie.key: cookie.value
                        for cookie in self._session.cookie_jar
                        if cookie.key in LOGIN_COOKIE_NAMES and _string(cookie.value)
                    },
                )
            except Exception:
                pass

        merged = merge_cookie_headers(self.cookie, cookie_header_from_values(values))
        filtered: dict[str, str] = {}
        parsed = SimpleCookie()
        try:
            parsed.load(merged)
            for name, morsel in parsed.items():
                if name in LOGIN_COOKIE_NAMES and _string(morsel.value):
                    filtered[name] = morsel.value
        except Exception:
            return merged
        return cookie_header_from_values(filtered)

    async def fetch_mentions(self, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._fetch_message_list(message_type="16", limit=limit)
        return _as_list(_as_dict(data.get("result")).get("messages"))

    async def fetch_comment_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._fetch_message_list(message_type="", limit=limit)
        messages = _as_list(_as_dict(data.get("result")).get("messages"))
        ret = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("message_type") or "") not in {"1", "2"}:
                continue
            if not get_link_id_from_message(message):
                continue
            if not get_reply_comment_id_from_message(message):
                continue
            ret.append(message)
        return ret

    async def _fetch_message_list(
        self,
        *,
        message_type: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id，请在平台配置中填写或提供 Cookie")
        params: dict[str, Any] = {
            "list_type": "0",
            "offset": "0",
            "limit": str(max(1, min(int(limit or 20), 50))),
            "heybox_id": self.heybox_id,
        }
        if message_type:
            params["message_type"] = str(message_type)
        else:
            params["no_more"] = "false"
        url = self.build_api_url(MESSAGE_API_PATH, params)
        data = await self._request_json("GET", url)
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "消息查询失败"))
        return data

    async def health_check(self) -> dict[str, Any]:
        messages = await self.fetch_mentions(limit=1)
        return {
            "ok": True,
            "heybox_id": self.heybox_id,
            "message_count": len(messages),
        }

    async def fetch_link_context(self, link_id: str) -> dict[str, Any] | None:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")
        url = self.build_api_url(
            LINK_TREE_API_PATH,
            {
                "h_src": "",
                "link_id": link_id,
                "is_first": "1",
                "page": "1",
                "index": "1",
                "limit": "20",
                "owner_only": "0",
                "heybox_id": self.heybox_id,
            },
        )
        data = await self._request_json("GET", url)
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "帖子详情查询失败"))
        return data

    async def submit_comment(
        self,
        link_id: str,
        reply_id: str,
        root_id: str,
        text: str,
        *,
        cooldown_seconds: int = 30,
    ) -> dict[str, Any]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")
        link_id = _string(link_id)
        reply_id = _string(reply_id)
        root_id = _string(root_id) or reply_id
        text = _string(text)
        if not link_id or not reply_id:
            raise XiaoHeiHeClientError("缺少 link_id 或 reply_id，无法评论回复")
        if not text:
            raise XiaoHeiHeClientError("回复文本为空，已取消发送")

        async with self._send_lock:
            cooldown = max(0, int(cooldown_seconds or 0))
            elapsed = time.time() - self._last_comment_sent_at
            if cooldown and elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)

            url = self.build_api_url(COMMENT_CREATE_API_PATH, {"heybox_id": self.heybox_id})
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": WEB_ORIGIN,
                "Referer": f"{WEB_ORIGIN}/",
            }
            body = {
                "is_cy": "0",
                "link_id": link_id,
                "reply_id": reply_id,
                "root_id": root_id,
                "text": text,
            }
            data = await self._request_json(
                "POST",
                url,
                data=urlencode(body),
                headers=headers,
            )
            if data.get("status") != "ok":
                raise XiaoHeiHeClientError(api_error_message(data, "评论发送失败"))
            self._last_comment_sent_at = time.time()
            return data

    async def send_text_to_session(
        self,
        session_id: str,
        text: str,
        *,
        cooldown_seconds: int = 30,
    ) -> dict[str, Any]:
        target = ReplyTarget.from_session_id(session_id)
        return await self.submit_comment(
            target.link_id,
            target.reply_id,
            target.root_id,
            text,
            cooldown_seconds=cooldown_seconds,
        )

    def normalize_incoming_message(
        self,
        message: dict[str, Any],
        *,
        source: str,
    ) -> IncomingMessage | None:
        link_id = get_link_id_from_message(message)
        direct_reply_id = get_reply_comment_id_from_message(message)
        root_id = get_root_comment_id_from_message(message)
        reply_id = direct_reply_id or root_id
        if not link_id or not reply_id:
            return None

        sender = _as_dict(message.get("user_a"))
        sender_id = get_user_id(sender)
        sender_name = get_user_display_name(sender) or sender_id or "小黑盒用户"
        message_id = _string(message.get("message_id")) or f"{link_id}:{reply_id}"
        root_id = root_id or reply_id
        target = ReplyTarget(
            link_id=link_id,
            reply_id=reply_id,
            root_id=root_id,
            sender_id=sender_id,
            message_id=message_id,
            source=source,
        )
        return IncomingMessage(
            message_id=message_id,
            source=source,
            text=get_message_text(message),
            sender_id=sender_id,
            sender_name=sender_name,
            timestamp=get_message_timestamp(message),
            link_id=link_id,
            reply_id=reply_id,
            root_id=root_id,
            session_id=target.to_session_id(),
            link_title=get_link_title(message),
            notification_text=get_notification_text(message),
            replied_text=get_replied_text(message),
            raw=message,
        )
