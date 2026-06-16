from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import mimetypes
import random
import re
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

import aiohttp


API_ORIGIN = "https://api.xiaoheihe.cn"
WEB_ORIGIN = "https://www.xiaoheihe.cn"
MESSAGE_API_PATH = "/bbs/app/user/message"
LINK_TREE_API_PATH = "/bbs/app/link/tree"
COMMENT_CREATE_API_PATH = "/bbs/app/comment/create"
DIRECT_MESSAGE_API_PATH = "/chatroom/v2/msg/user"
STRANGER_DIRECT_MESSAGE_API_PATH = "/chat/stranger_messages/"
IMAGE_COPY_BY_URL_API_PATH = "/bbs/app/api/qcloud/cos/copy/image/by/url"
COS_UPLOAD_INFO_API_PATH = "/bbs/app/api/qcloud/cos/upload/info/v2"
COS_UPLOAD_TOKEN_API_PATH = "/bbs/app/api/qcloud/cos/upload/token/v2"
COS_UPLOAD_CALLBACK_API_PATH = "/bbs/app/api/qcloud/cos/upload/callback/v2"
QRCODE_URL_API_PATH = "/account/get_qrcode_url/"
QR_STATE_API_PATH = "/account/qr_state/"
DEFAULT_COS_REGION = "ap-shanghai"
LOCAL_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}

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
class DirectMessageTarget:
    user_id: str

    def to_session_id(self) -> str:
        return "!".join(["dm", _escape_session_part(self.user_id)])

    @classmethod
    def from_session_id(cls, session_id: str) -> "DirectMessageTarget":
        parts = str(session_id or "").split("!", 1)
        if len(parts) != 2 or parts[0] != "dm":
            raise ValueError(f"unsupported XiaoHeiHe direct message session id: {session_id!r}")
        user_id = _unescape_session_part(parts[1])
        if not user_id:
            raise ValueError(f"unsupported XiaoHeiHe direct message session id: {session_id!r}")
        return cls(user_id=user_id)


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
    rich_text: dict[str, Any] = field(default_factory=dict)
    image_urls: list[str] = field(default_factory=list)
    replied_image_urls: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    @property
    def target_key(self) -> str:
        if self.session_id.startswith("dm!"):
            return f"{self.source}::{self.session_id}::{self.message_id or self.timestamp}"
        return f"{self.link_id}::{self.reply_id}"


@dataclass
class LinkContext:
    text: str = ""
    rich_text: dict[str, Any] = field(default_factory=dict)
    image_urls: list[str] = field(default_factory=list)
    comment_image_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocalImageInfo:
    path: Path
    name: str
    mimetype: str
    size: int
    width: int = 0
    height: int = 0
    duration: int = 0


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


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(_string(value) for value in values if _string(value)))


def _decode_htmlish_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\\u003c", "<").replace("\\u003C", "<")
    text = text.replace("\\u003e", ">").replace("\\u003E", ">")
    text = text.replace("\\u0026", "&")
    return html.unescape(text)


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
    text = _decode_htmlish_text(_extract_rich_text(value))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\[cube_([^\]]+)\]", r"[\1]", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_http_url(value: Any) -> bool:
    return bool(re.match(r"^https?://", _string(value), flags=re.I))


def _is_file_url(value: Any) -> bool:
    return urlparse(_string(value)).scheme.lower() == "file"


def _local_path_from_value(value: Any) -> Path | None:
    text = _string(value)
    if not text or _is_http_url(text):
        return None

    if _is_file_url(text):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path or "")
        if parsed.netloc and raw_path:
            raw_path = f"//{parsed.netloc}{raw_path}"
        elif parsed.netloc:
            raw_path = parsed.netloc
        if re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        if not raw_path:
            return None
        path = Path(raw_path)
    else:
        path = Path(text)

    try:
        return path.expanduser()
    except Exception:
        return path


def _looks_like_local_image_path(value: Any) -> bool:
    path = _local_path_from_value(value)
    if path is None:
        return False
    return path.suffix.lower() in LOCAL_IMAGE_EXTENSIONS


def _read_png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    return 0, 0


def _read_gif_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    return 0, 0


def _read_bmp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 26 and data.startswith(b"BM"):
        width = struct.unpack("<I", data[18:22])[0]
        height = abs(struct.unpack("<i", data[22:26])[0])
        return width, height
    return 0, 0


def _read_webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        if data[23:26] == b"\x9d\x01\x2a":
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return width, height
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    return 0, 0


def _read_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return 0, 0

    index = 2
    while index + 9 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break

        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[index : index + 2])[0]
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        } and segment_length >= 7:
            height = struct.unpack(">H", data[index + 3 : index + 5])[0]
            width = struct.unpack(">H", data[index + 5 : index + 7])[0]
            return width, height
        index += segment_length
    return 0, 0


def _read_image_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as fp:
            data = fp.read(512 * 1024)
    except OSError:
        return 0, 0

    for reader in (
        _read_png_dimensions,
        _read_jpeg_dimensions,
        _read_gif_dimensions,
        _read_webp_dimensions,
        _read_bmp_dimensions,
    ):
        width, height = reader(data)
        if width > 0 and height > 0:
            return width, height
    return 0, 0


def _get_local_image_info(value: Any) -> LocalImageInfo:
    path = _local_path_from_value(value)
    if path is None:
        raise XiaoHeiHeClientError(f"不支持的图片地址: {_string(value)!r}")
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise XiaoHeiHeClientError(f"本地图片不存在: {path}") from exc
    except OSError as exc:
        raise XiaoHeiHeClientError(f"无法读取本地图片路径: {path}") from exc
    if not path.is_file():
        raise XiaoHeiHeClientError(f"本地图片不是文件: {path}")

    stat = path.stat()
    mimetype = mimetypes.guess_type(str(path))[0] or ""
    if not mimetype.startswith("image/"):
        raise XiaoHeiHeClientError(f"不支持的本地图片类型: {path.name}")
    width, height = _read_image_dimensions(path)
    return LocalImageInfo(
        path=path,
        name=path.name,
        mimetype=mimetype,
        size=stat.st_size,
        width=width,
        height=height,
    )


def _cos_quote(value: str) -> str:
    return quote(value, safe="/-_.~")


def _hmac_sha1(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha1).hexdigest()


def _cos_authorization(
    *,
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    headers: dict[str, str],
    start_time: int,
    end_time: int,
) -> str:
    method_lower = method.lower()
    key_time = f"{start_time};{end_time}"
    header_items = {
        key.lower(): " ".join(str(value).strip().split())
        for key, value in headers.items()
        if str(value).strip()
    }
    sorted_header_keys = sorted(header_items)
    signed_headers = ";".join(sorted_header_keys)
    http_headers = "&".join(
        f"{quote(key, safe='-_.~')}={quote(header_items[key], safe='-_.~')}"
        for key in sorted_header_keys
    )
    http_string = "\n".join(
        [
            method_lower,
            _cos_quote(path),
            "",
            http_headers,
            "",
        ],
    )
    sign_key = _hmac_sha1(secret_key.encode("utf-8"), key_time)
    string_to_sign = "\n".join(
        [
            "sha1",
            key_time,
            hashlib.sha1(http_string.encode("utf-8")).hexdigest(),
            "",
        ],
    )
    signature = _hmac_sha1(sign_key.encode("utf-8"), string_to_sign)
    return (
        "q-sign-algorithm=sha1&"
        f"q-ak={secret_id}&"
        f"q-sign-time={key_time}&"
        f"q-key-time={key_time}&"
        f"q-header-list={signed_headers}&"
        "q-url-param-list=&"
        f"q-signature={signature}"
    )


def _extract_image_urls_from_html(value: Any) -> list[str]:
    text = _decode_htmlish_text(value)
    urls = []
    pattern = re.compile(
        r"<img\b[^>]*\b(?:data-original|data-src|src)=([\"'])(.*?)\1",
        flags=re.I,
    )
    for match in pattern.finditer(text):
        urls.append(match.group(2))
    return _unique_strings(url for url in urls if _is_http_url(url))


def extract_image_urls(value: Any) -> list[str]:
    urls: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            text = _decode_htmlish_text(item)
            if _is_http_url(text):
                urls.append(text)
            urls.extend(_extract_image_urls_from_html(text))
            parsed = _parse_rich_text_json(text)
            if parsed is not None:
                visit(parsed)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            for key in (
                "url",
                "thumb",
                "src",
                "image",
                "image_url",
                "img_url",
                "original",
                "data_original",
                "data-src",
                "data-original",
            ):
                url = item.get(key)
                if _is_http_url(url):
                    urls.append(_string(url))

            for key in (
                "text",
                "content",
                "html",
                "value",
                "children",
                "items",
                "segments",
                "spans",
                "blocks",
                "imgs",
                "images",
            ):
                if key in item:
                    visit(item.get(key))

    visit(value)
    return _unique_strings(urls)


def extract_primary_image_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        urls = []
        for item in value:
            if isinstance(item, dict):
                url = item.get("url") or item.get("src") or item.get("image") or item.get("thumb")
                if _is_http_url(url):
                    urls.append(_string(url))
                    continue
            urls.extend(extract_image_urls(item))
        return _unique_strings(urls)

    if isinstance(value, dict):
        url = value.get("url") or value.get("src") or value.get("image") or value.get("thumb")
        if _is_http_url(url):
            return [_string(url)]

    return extract_image_urls(value)


def _looks_like_xiaoheihe_image_url(value: Any) -> bool:
    text = _string(value)
    if not _is_http_url(text):
        return False
    host = urlparse(text).netloc.lower()
    return host.endswith("max-c.com") or host.endswith("myqcloud.com")


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


def get_message_rich_text(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment_a_text": message.get("comment_a_text"),
        "comment_text": message.get("comment_text"),
        "content": message.get("content"),
        "text": message.get("text"),
        "comment_b_text": message.get("comment_b_text"),
    }


def get_message_image_urls(message: dict[str, Any]) -> list[str]:
    return extract_image_urls(
        [
            message.get("imgs"),
            message.get("images"),
            message.get("comment_a_imgs"),
            message.get("comment_images"),
            message.get("comment_a_text"),
            message.get("comment_text"),
            message.get("content"),
        ],
    )


def get_comment_image_urls(comment: Any) -> list[str]:
    comment = _as_dict(comment)
    return extract_primary_image_urls(
        [
            comment.get("imgs"),
            comment.get("images"),
            comment.get("comment_imgs"),
            comment.get("comment_images"),
        ],
    )


def get_replied_image_urls(message: dict[str, Any]) -> list[str]:
    return extract_image_urls(
        [
            message.get("comment_b_imgs"),
            message.get("reply_imgs"),
            message.get("comment_b_text"),
        ],
    )


def get_notification_text(message: dict[str, Any]) -> str:
    return strip_html(message.get("text") or "")


def get_replied_text(message: dict[str, Any]) -> str:
    return strip_html(message.get("comment_b_text") or "")


def get_link_title(message: dict[str, Any]) -> str:
    link = _as_dict(message.get("link"))
    return _string(link.get("title"))


def get_direct_message_user_id(item: Any) -> str:
    item = _as_dict(item)
    user = _as_dict(
        item.get("user_a")
        or item.get("user")
        or item.get("recipient_info")
        or item.get("sender_info"),
    )
    return _string(
        get_user_id(user)
        or item.get("to_user_id")
        or item.get("target_user_id")
        or item.get("user_id")
        or item.get("userid")
        or (_string(item.get("message_id")) if _string(item.get("entry")) == "message" else ""),
    )


def get_direct_message_sender_id(message: dict[str, Any]) -> str:
    sender = _as_dict(
        message.get("sender")
        or message.get("sender_info")
        or message.get("user")
        or message.get("user_a"),
    )
    return _string(
        message.get("sender_id")
        or message.get("from_user_id")
        or message.get("from_uid")
        or get_user_id(sender),
    )


def get_direct_message_timestamp(message: dict[str, Any]) -> int:
    for key in ("send_time", "timestamp", "time", "update_time", "created_at", "create_time"):
        value = _number(message.get(key))
        if value > 0:
            return int(value / 1000 if value > 100_000_000_000 else value)
    return int(time.time())


def get_direct_message_text(message: dict[str, Any]) -> str:
    return strip_html(
        message.get("content")
        or message.get("msg")
        or message.get("text")
        or message.get("message")
        or "",
    )


def get_direct_message_image_urls(message: dict[str, Any]) -> list[str]:
    urls = extract_image_urls(
        [
            message.get("img"),
            message.get("imgs"),
            message.get("image"),
            message.get("images"),
            message.get("content"),
        ],
    )
    for value in (message.get("img"), message.get("imgs")):
        text = _decode_htmlish_text(value)
        for match in re.finditer(r"https?://[^\s,;|\"'<>()]+", text, flags=re.I):
            urls.append(match.group(0).rstrip("，。,.；;"))
    return _unique_strings(urls)


def get_direct_message_rich_text(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": message.get("content"),
        "msg": message.get("msg"),
        "text": message.get("text"),
        "img": message.get("img"),
        "msg_type": message.get("msg_type"),
        "seq": message.get("seq"),
    }


def get_direct_message_id(message: dict[str, Any], peer_user_id: str) -> str:
    sender_id = get_direct_message_sender_id(message)
    seq = _string(message.get("seq") or message.get("msg_seq") or message.get("sequence"))
    explicit_id = _string(
        message.get("msg_id")
        or message.get("message_id")
        or message.get("id")
        or message.get("_id"),
    )
    if explicit_id:
        return f"dm:{peer_user_id}:{explicit_id}"
    if seq:
        return f"dm:{peer_user_id}:{seq}"
    digest = _md5(
        json.dumps(
            {
                "sender": sender_id,
                "time": get_direct_message_timestamp(message),
                "text": get_direct_message_text(message),
                "images": get_direct_message_image_urls(message),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )[:12]
    return f"dm:{peer_user_id}:{digest}"


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


def get_link_image_urls(link: dict[str, Any]) -> list[str]:
    return extract_image_urls(
        [
            link.get("imgs"),
            link.get("thumbs"),
            link.get("images"),
            link.get("image"),
            link.get("text"),
            link.get("description"),
        ],
    )


def summarize_link_context_data(
    data: dict[str, Any] | None,
    *,
    max_comment_lines: int = 8,
) -> LinkContext:
    if not data:
        return LinkContext()
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
    comment_image_urls: list[str] = []
    for group in normalize_comment_groups(data):
        comments = [_as_dict(group.get("root")), *_as_list(group.get("replies"))]
        for comment in comments:
            comment_dict = _as_dict(comment)
            line = get_comment_line(comment_dict)
            if line:
                lines.append(line)
            comment_image_urls.extend(get_comment_image_urls(comment_dict))

    if lines:
        selected = lines[: max(1, int(max_comment_lines or 8))]
        parts.append("评论区摘要：\n" + "\n".join(selected))

    return LinkContext(
        text="\n\n".join(parts),
        rich_text={
            "link_text": link.get("text"),
            "link_description": link.get("description"),
            "comments": [
                {
                    "id": get_comment_id(comment),
                    "text": comment.get("text") or comment.get("content"),
                    "imgs": comment.get("imgs"),
                }
                for group in normalize_comment_groups(data)
                for comment in [_as_dict(group.get("root")), *_as_list(group.get("replies"))]
                if isinstance(comment, dict)
            ],
        },
        image_urls=get_link_image_urls(link),
        comment_image_urls=_unique_strings(comment_image_urls),
    )


def summarize_link_context(
    data: dict[str, Any] | None,
    *,
    max_comment_lines: int = 8,
) -> str:
    return summarize_link_context_data(
        data,
        max_comment_lines=max_comment_lines,
    ).text


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
        self._last_direct_message_sent_at = 0.0
        self._direct_message_ack_id = int(time.time() * 1000) % 1_000_000_000
        if self.device_id:
            self.api_params.setdefault("device_id", self.device_id)

    def is_self_user_id(self, user_id: Any) -> bool:
        return bool(self.heybox_id and _string(user_id) == self.heybox_id)

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

    async def fetch_direct_message_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id，请在平台配置中填写或提供 Cookie")
        params: dict[str, Any] = {
            "list_type": "2",
            "offset": "0",
            "limit": str(max(1, min(int(limit or 20), 50))),
            "heybox_id": self.heybox_id,
        }
        data = await self._request_json("GET", self.build_api_url(MESSAGE_API_PATH, params))
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "私信列表查询失败"))
        messages = _as_list(_as_dict(data.get("result")).get("messages"))
        return [
            message
            for message in messages
            if (
                isinstance(message, dict)
                and _string(message.get("entry")) == "message"
                and get_direct_message_user_id(message)
            )
        ]

    async def fetch_stranger_direct_message_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id，请在平台配置中填写或提供 Cookie")
        params: dict[str, Any] = {
            "offset": "0",
            "limit": str(max(1, min(int(limit or 20), 50))),
            "heybox_id": self.heybox_id,
        }
        data = await self._request_json(
            "GET",
            self.build_api_url(STRANGER_DIRECT_MESSAGE_API_PATH, params),
        )
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "陌生人私信列表查询失败"))
        result = _as_dict(data.get("result"))
        raw_items = (
            result.get("list")
            or result.get("messages")
            or result.get("items")
            or data.get("list")
            or []
        )
        return [
            item
            for item in _as_list(raw_items)
            if isinstance(item, dict) and get_direct_message_user_id(item)
        ]

    async def fetch_direct_messages(
        self,
        to_user_id: Any,
        *,
        limit: int = 30,
        seq: Any = None,
    ) -> list[dict[str, Any]]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id，请在平台配置中填写或提供 Cookie")
        target_user_id = _string(to_user_id)
        if not target_user_id:
            return []
        params: dict[str, Any] = {
            "to_user_id": target_user_id,
            "offset": "0",
            "limit": str(max(1, min(int(limit or 30), 50))),
            "heybox_id": self.heybox_id,
        }
        if _string(seq):
            params["seq"] = _string(seq)
        data = await self._request_json(
            "GET",
            self.build_api_url(DIRECT_MESSAGE_API_PATH, params),
        )
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "私信会话查询失败"))
        result = _as_dict(data.get("result"))
        return [
            message
            for message in _as_list(result.get("list"))
            if isinstance(message, dict)
        ]

    async def fetch_direct_messages_from_recent(
        self,
        *,
        limit: int = 20,
        conversation_limit: int = 30,
        include_strangers: bool = False,
    ) -> list[IncomingMessage]:
        entries = [
            (entry, "direct_message")
            for entry in await self.fetch_direct_message_entries(limit=limit)
        ]
        if include_strangers:
            entries.extend(
                (entry, "stranger_direct_message")
                for entry in await self.fetch_stranger_direct_message_entries(limit=limit)
            )

        incoming: list[IncomingMessage] = []
        seen_user_ids: set[str] = set()
        for entry, source in entries:
            user_id = get_direct_message_user_id(entry)
            if not user_id or user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            history = await self.fetch_direct_messages(
                user_id,
                limit=conversation_limit,
            )
            for message in history:
                normalized = self.normalize_direct_message(
                    message,
                    peer_user_id=user_id,
                    peer_info=entry.get("user_a") or entry,
                    source=source,
                )
                if normalized is not None:
                    incoming.append(normalized)
        return incoming

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

    def _form_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
        }

    async def prepare_comment_image_urls(self, image_urls: Iterable[Any]) -> list[str]:
        prepared: list[str] = []
        for image_url in _unique_strings(image_urls):
            if _is_http_url(image_url):
                if _looks_like_xiaoheihe_image_url(image_url):
                    prepared.append(image_url)
                    continue
                prepared.append(await self.copy_image_by_url(image_url))
                continue
            if _looks_like_local_image_path(image_url):
                prepared.append(await self.upload_local_image_to_cos(image_url))
        return _unique_strings(prepared)

    async def copy_image_by_url(self, image_url: str) -> str:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")
        image_url = _string(image_url)
        if not _is_http_url(image_url):
            raise XiaoHeiHeClientError(f"不支持的图片地址: {image_url!r}")

        url = self.build_api_url(
            IMAGE_COPY_BY_URL_API_PATH,
            {
                "heybox_id": self.heybox_id,
                "target_url": image_url,
                "watermark": "false",
            },
        )
        data = await self._request_json("GET", url)
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "图片转存失败"))
        result = _as_dict(data.get("result"))
        copied_url = _string(result.get("url") or result.get("preview_url"))
        if not copied_url:
            raise XiaoHeiHeClientError("图片转存响应缺少 url")
        return copied_url

    async def upload_local_image_to_cos(self, image_path: Any) -> str:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")

        image = _get_local_image_info(image_path)
        upload_info = await self._request_cos_upload_info(image)
        keys = _as_list(upload_info.get("keys"))
        key = _string(keys[0] if keys else upload_info.get("key"))
        bucket = _string(upload_info.get("bucket"))
        region = _string(upload_info.get("region")) or DEFAULT_COS_REGION
        if not key or not bucket:
            raise XiaoHeiHeClientError("图片上传初始化响应缺少 bucket 或 key")

        token = await self._request_cos_upload_token(
            bucket=bucket,
            keys=[key],
            mimetypes=[image.mimetype],
        )
        await self._put_cos_object(
            image=image,
            bucket=bucket,
            region=region,
            key=key,
            token=token,
        )
        return await self._finish_cos_upload([key])

    async def _request_cos_upload_info(self, image: LocalImageInfo) -> dict[str, Any]:
        file_info = {
            "name": image.name,
            "mimetype": image.mimetype,
            "fsize": image.size,
            "width": image.width,
            "height": image.height,
            "duration": image.duration,
        }
        body = urlencode(
            {
                "file_infos": json.dumps([file_info], ensure_ascii=False, separators=(",", ":")),
                "scope": "bbs",
                "need_cache": "0",
            },
        )
        data = await self._request_json(
            "POST",
            self.build_api_url(COS_UPLOAD_INFO_API_PATH, {"heybox_id": self.heybox_id}),
            data=body,
            headers=self._form_headers(),
        )
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "图片上传初始化失败"))
        return _as_dict(data.get("result"))

    async def _request_cos_upload_token(
        self,
        *,
        bucket: str,
        keys: list[str],
        mimetypes: list[str],
    ) -> dict[str, Any]:
        body = urlencode(
            {
                "bucket": bucket,
                "keys": json.dumps(keys, ensure_ascii=False, separators=(",", ":")),
                "mimetypes": json.dumps(mimetypes, ensure_ascii=False, separators=(",", ":")),
                "is_multipart_upload": "0",
            },
        )
        data = await self._request_json(
            "POST",
            self.build_api_url(COS_UPLOAD_TOKEN_API_PATH, {"heybox_id": self.heybox_id}),
            data=body,
            headers=self._form_headers(),
        )
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "图片上传授权失败"))
        result = _as_dict(data.get("result"))
        credentials = _as_dict(result.get("credentials"))
        if not (
            _string(credentials.get("tmpSecretId"))
            and _string(credentials.get("tmpSecretKey"))
            and _string(credentials.get("sessionToken"))
        ):
            raise XiaoHeiHeClientError("图片上传授权响应缺少临时凭证")
        return result

    async def _put_cos_object(
        self,
        *,
        image: LocalImageInfo,
        bucket: str,
        region: str,
        key: str,
        token: dict[str, Any],
    ) -> None:
        credentials = _as_dict(token.get("credentials"))
        secret_id = _string(credentials.get("tmpSecretId"))
        secret_key = _string(credentials.get("tmpSecretKey"))
        session_token = _string(credentials.get("sessionToken"))
        if not secret_id or not secret_key or not session_token:
            raise XiaoHeiHeClientError("图片上传授权响应缺少临时凭证")

        now = int(time.time())
        start_time = int(_number(token.get("startTime")) or max(0, now - 60))
        end_time = int(_number(token.get("expiredTime")) or (now + 300))
        host = f"{bucket}.cos.{region}.myqcloud.com"
        object_path = "/" + key.lstrip("/")
        headers = {
            "Host": host,
            "Content-Type": image.mimetype,
            "x-cos-security-token": session_token,
        }
        headers["Authorization"] = _cos_authorization(
            secret_id=secret_id,
            secret_key=secret_key,
            method="PUT",
            path=object_path,
            headers=headers,
            start_time=start_time,
            end_time=end_time,
        )
        url = f"https://{host}{_cos_quote(object_path)}"
        try:
            payload = image.path.read_bytes()
        except OSError as exc:
            raise XiaoHeiHeClientError(f"读取本地图片失败: {image.path}") from exc

        timeout = aiohttp.ClientTimeout(total=max(self.timeout, 30))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, data=payload, headers=headers) as response:
                if response.status >= 400:
                    text = await response.text()
                    raise XiaoHeiHeClientError(
                        f"COS 图片上传失败: HTTP {response.status}, {text[:200]}",
                    )

    async def _finish_cos_upload(self, keys: list[str]) -> str:
        body = urlencode(
            {
                "keys": json.dumps(keys, ensure_ascii=False, separators=(",", ":")),
            },
        )
        data = await self._request_json(
            "POST",
            self.build_api_url(
                COS_UPLOAD_CALLBACK_API_PATH,
                {
                    "heybox_id": self.heybox_id,
                    "is_finished": "true",
                },
            ),
            data=body,
            headers=self._form_headers(),
        )
        if data.get("status") != "ok":
            raise XiaoHeiHeClientError(api_error_message(data, "图片上传回调失败"))
        result = _as_dict(data.get("result"))
        preview_urls = _as_list(result.get("preview_urls"))
        thumbs = _as_list(result.get("thumbs"))
        image_url = _string(preview_urls[0] if preview_urls else "")
        if not image_url:
            image_url = _string(thumbs[0] if thumbs else "")
        if not image_url:
            raise XiaoHeiHeClientError("图片上传回调响应缺少 preview_url")
        return image_url

    async def submit_comment(
        self,
        link_id: str,
        reply_id: str,
        root_id: str,
        text: str,
        *,
        image_urls: Iterable[Any] | None = None,
        cooldown_seconds: int = 30,
    ) -> dict[str, Any]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")
        link_id = _string(link_id)
        reply_id = _string(reply_id)
        root_id = _string(root_id) or reply_id
        text = _string(text)
        prepared_image_urls = await self.prepare_comment_image_urls(image_urls or [])
        if not link_id or not reply_id:
            raise XiaoHeiHeClientError("缺少 link_id 或 reply_id，无法评论回复")
        if not text and not prepared_image_urls:
            raise XiaoHeiHeClientError("回复内容为空，已取消发送")

        async with self._send_lock:
            cooldown = max(0, int(cooldown_seconds or 0))
            elapsed = time.time() - self._last_comment_sent_at
            if cooldown and elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)

            url = self.build_api_url(COMMENT_CREATE_API_PATH, {"heybox_id": self.heybox_id})
            body = {
                "is_cy": "0",
                "link_id": link_id,
                "reply_id": reply_id,
                "root_id": root_id,
                "text": text,
            }
            if prepared_image_urls:
                body["imgs"] = ";".join(prepared_image_urls)
            data = await self._request_json(
                "POST",
                url,
                data=urlencode(body),
                headers=self._form_headers(),
            )
            if data.get("status") != "ok":
                raise XiaoHeiHeClientError(api_error_message(data, "评论发送失败"))
            self._last_comment_sent_at = time.time()
            return data

    async def submit_direct_message(
        self,
        to_user_id: Any,
        text: str,
        *,
        image_urls: Iterable[Any] | None = None,
        cooldown_seconds: int = 5,
    ) -> dict[str, Any]:
        if not self.heybox_id:
            raise XiaoHeiHeClientError("缺少 heybox_id")
        target_user_id = _string(to_user_id)
        text = _string(text)
        prepared_image_urls = await self.prepare_comment_image_urls(image_urls or [])
        if not target_user_id:
            raise XiaoHeiHeClientError("缺少私信目标用户 ID")
        if not text and not prepared_image_urls:
            raise XiaoHeiHeClientError("私信内容为空，已取消发送")

        async with self._send_lock:
            cooldown = max(0, int(cooldown_seconds or 0))
            elapsed = time.time() - self._last_direct_message_sent_at
            if cooldown and elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)

            self._direct_message_ack_id += 1
            url = self.build_api_url(
                DIRECT_MESSAGE_API_PATH,
                {
                    "to_user_id": target_user_id,
                    "heybox_id": self.heybox_id,
                },
            )
            body = {
                "heybox_ack_id": str(self._direct_message_ack_id),
                "img": "".join(prepared_image_urls),
                "msg": text,
                "msg_type": "6",
            }
            data = await self._request_json(
                "POST",
                url,
                data=urlencode(body),
                headers=self._form_headers(),
            )
            if data.get("status") != "ok":
                raise XiaoHeiHeClientError(api_error_message(data, "私信发送失败"))
            self._last_direct_message_sent_at = time.time()
            return data

    async def send_text_to_session(
        self,
        session_id: str,
        text: str,
        *,
        image_urls: Iterable[Any] | None = None,
        cooldown_seconds: int = 30,
    ) -> dict[str, Any]:
        if str(session_id or "").startswith("dm!"):
            target = DirectMessageTarget.from_session_id(session_id)
            return await self.submit_direct_message(
                target.user_id,
                text,
                image_urls=image_urls,
                cooldown_seconds=cooldown_seconds,
            )
        target = ReplyTarget.from_session_id(session_id)
        return await self.submit_comment(
            target.link_id,
            target.reply_id,
            target.root_id,
            text,
            image_urls=image_urls,
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
            rich_text=get_message_rich_text(message),
            image_urls=get_message_image_urls(message),
            replied_image_urls=get_replied_image_urls(message),
            raw=message,
        )

    def normalize_direct_message(
        self,
        message: dict[str, Any],
        *,
        peer_user_id: Any,
        peer_info: Any = None,
        source: str = "direct_message",
    ) -> IncomingMessage | None:
        peer_user_id_text = _string(peer_user_id)
        if not peer_user_id_text:
            return None

        sender_id = get_direct_message_sender_id(message)
        if self.is_self_user_id(sender_id):
            return None

        sender_info = _as_dict(
            message.get("sender")
            or message.get("sender_info")
            or message.get("user")
            or message.get("user_a"),
        )
        peer_info_dict = _as_dict(peer_info)
        sender_name = (
            get_user_display_name(sender_info)
            or get_user_display_name(peer_info_dict)
            or sender_id
            or peer_user_id_text
            or "小黑盒用户"
        )
        text = get_direct_message_text(message)
        image_urls = get_direct_message_image_urls(message)
        if not text and not image_urls:
            return None

        session = DirectMessageTarget(peer_user_id_text)
        message_id = get_direct_message_id(message, peer_user_id_text)
        return IncomingMessage(
            message_id=message_id,
            source=source,
            text=text,
            sender_id=sender_id or peer_user_id_text,
            sender_name=sender_name,
            timestamp=get_direct_message_timestamp(message),
            link_id="",
            reply_id="",
            root_id="",
            session_id=session.to_session_id(),
            notification_text=text,
            rich_text=get_direct_message_rich_text(message),
            image_urls=image_urls,
            raw=message,
        )
