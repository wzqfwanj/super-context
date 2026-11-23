#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「超级上下文」后端服务：上传资料 → 自动生成描述 → 智能体按链接取用。

只使用 Python 标准库。启动：

    python3 server.py [port]

环境变量：
    HOST                    监听地址，默认 127.0.0.1
    PORT                    监听端口，默认 8082（命令行参数优先）
    CONTEXT_DATA            数据目录，默认 ./data（items.json + files/）
    CONTEXT_WEB             静态页面目录，默认 ./web
    CONTEXT_PUBLIC_PREFIX   对外访问前缀，默认 http://127.0.0.1:<port>
                            线上为 https://wkeeli.cn/context
    CONTEXT_MAX_UPLOAD_MB   单个上传文件上限（MB），默认 200
    CONTEXT_API_KEY 等      由 describe.py 读取，见 docs/CONTRACT.md

对外接口见 docs/CONTRACT.md 第 4 节。
"""

from __future__ import annotations

import copy
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

try:  # 描述生成流水线（media-pipeline 交付）；缺失时服务仍可运行并给出兜底描述
    import describe as describe_module
except Exception:  # pragma: no cover - 仅在模块缺失/损坏时触发
    describe_module = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8082
DEFAULT_MAX_UPLOAD_MB = 200
MAX_JSON_BYTES = 1024 * 1024       # PATCH 等 JSON 请求体上限 1MB
MAX_DRAIN_BYTES = 8 * 1024 * 1024  # 413 后最多丢弃的请求体字节数
TZ8 = timezone(timedelta(hours=8))  # 时间戳统一 UTC+8

KINDS = ("image", "document", "audio", "video", "other")
KIND_LABELS = {"image": "图片", "document": "文档", "audio": "音频", "video": "视频", "other": "其他"}
STATUSES = ("processing", "ready", "error")

# 没有 describe.py 时的兜底分类（只影响 UI 标签，不影响文件本身）
_FALLBACK_EXT_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".avif", ".tiff", ".ico"},
    "document": {
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
        ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".py", ".java", ".go", ".rs", ".c", ".cpp",
        ".h", ".hpp", ".sh", ".ps1", ".bat", ".ini", ".toml", ".conf", ".sql", ".tex",
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub",
    },
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".aiff"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp"},
}

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# ---------------------------------------------------------------- 工具函数

def now_iso() -> str:
    """当前时间，UTC+8 的 ISO8601 秒级字符串。"""
    return datetime.now(TZ8).isoformat(timespec="seconds")


def log_out(message: str) -> None:
    """信息日志 -> stdout（journald 收集）。"""
    print(message, file=sys.stdout, flush=True)


def log_err(message: str) -> None:
    """错误日志 -> stderr。"""
    print(message, file=sys.stderr, flush=True)


def fmt_time(value: str) -> str:
    """ISO8601 -> YYYY-MM-DD HH:MM（导出/清单里用）。"""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        moment = datetime.now(TZ8)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ8)
    return moment.astimezone(TZ8).strftime("%Y-%m-%d %H:%M")


def fmt_size(size) -> str:
    """字节数 -> 可读大小。"""
    try:
        number = float(size)
    except (TypeError, ValueError):
        return "未知大小"
    if number < 1024:
        return "%d B" % number
    for unit in ("KB", "MB", "GB", "TB"):
        number /= 1024.0
        if number < 1024:
            return "%.1f %s" % (number, unit)
    return "%.1f PB" % number


def md_alt(name: str) -> str:
    """Markdown 图片的 alt 文本：去掉会破坏语法的方括号与换行。"""
    return str(name).replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def classify_name(name: str) -> str:
    """文件类型：优先用 describe.classify，缺失时用内置扩展名表。"""
    if describe_module is not None:
        try:
            kind = describe_module.classify(name)
            if kind in KINDS:
                return kind
        except Exception:
            pass
    ext = os.path.splitext(name)[1].lower()
    for kind in ("image", "document", "audio", "video"):
        if ext in _FALLBACK_EXT_KINDS[kind]:
            return kind
    return "other"


def guess_mime(name: str) -> str:
    """MIME：优先用 describe.guess_mime，缺失时用 mimetypes。"""
    if describe_module is not None:
        try:
            mime = describe_module.guess_mime(name)
            if isinstance(mime, str) and mime:
                return mime
        except Exception:
            pass
    mime = mimetypes.guess_type(name)[0]
    return mime or "application/octet-stream"


def sanitize_filename(raw) -> str:
    """去掉路径与控制字符，限制长度；返回空串表示非法。"""
    if not isinstance(raw, str):
        return ""
    name = raw.replace("\\", "/")
    name = name.rsplit("/", 1)[-1]           # 只保留 basename
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if not name or name in (".", ".."):
        return ""
    return name[:120]


def safe_ext(name: str) -> str:
    """只保留 [.a-z0-9] 字母数字扩展名（≤10 字符），避免奇怪后缀。"""
    ext = os.path.splitext(name)[1].lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", ext or ""):
        return ext
    return ""


class BadRequest(Exception):
    """请求格式错误（400）。"""


class RequestTooLarge(Exception):
    """请求体过大（413）。"""

    def __init__(self, length: int) -> None:
        super().__init__("请求体过大")
        self.length = length


class NotFound(Exception):
    """资源不存在（404）。"""


class ValidationError(Exception):
    """字段校验失败（400）。"""


class MethodNotAllowed(Exception):
    """方法不允许（405）。"""

    def __init__(self, allow: str) -> None:
        super().__init__("方法不允许")
        self.allow = allow


# ---------------------------------------------------------------- 数据存储

def _normalize_item(entry) -> dict | None:
    """把任意 JSON 条目规整成内部 item；无法修复的返回 None。"""
    if not isinstance(entry, dict):
        return None
    item_id = entry.get("id")
    name = entry.get("name")
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-f]{4,32}", item_id):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    name = sanitize_filename(name) or "未命名"
    kind = entry.get("kind")
    if kind not in KINDS:
        kind = classify_name(name)
    mime = entry.get("mime")
    if not isinstance(mime, str) or not mime:
        mime = guess_mime(name)
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    description = entry.get("description")
    if not isinstance(description, str):
        description = ""
    content = entry.get("content")
    if not isinstance(content, str):
        content = ""
    status = entry.get("status")
    if status not in STATUSES:
        status = "ready"
    error = entry.get("error")
    if not isinstance(error, str):
        error = ""
    if status == "processing":
        # 服务重启时队列已丢失，标记为失败，让用户重新上传
        status = "error"
        error = error or "服务重启，描述未完成，请重新上传"
    ext = entry.get("ext")
    if not isinstance(ext, str):
        ext = ""
    created_at = entry.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = now_iso()
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        updated_at = created_at
    return {
        "id": item_id,
        "name": name,
        "kind": kind,
        "mime": mime,
        "size": size,
        "ext": ext,
        "description": description,
        "content": content,
        "status": status,
        "error": error,
        "created_at": created_at,
        "updated_at": updated_at,
    }


class ItemStore:
    """items.json 持久化 + files/ 文件目录，线程安全，原子落盘。"""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.files_dir = os.path.join(self.data_dir, "files")
        self.items_path = os.path.join(self.data_dir, "items.json")
        self.lock = threading.Lock()
        os.makedirs(self.files_dir, exist_ok=True)
        self.token = ""
        self.items: list[dict] = []
        self.updated_at = now_iso()
        self._load()

    # ---- 读写 ----

    def _load(self) -> None:
        raw = None
        if os.path.exists(self.items_path):
            try:
                with open(self.items_path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError) as exc:
                backup = self.items_path + ".corrupt"
                try:
                    os.replace(self.items_path, backup)
                except OSError:
                    backup = "(备份失败)"
                log_err("[%s] items.json 无法读取，已重新初始化（备份：%s）：%s"
                        % (now_iso(), backup, exc))
        token = ""
        items = []
        updated_at = now_iso()
        if isinstance(raw, dict):
            raw_token = raw.get("token")
            if isinstance(raw_token, str) and re.fullmatch(r"[0-9a-f]{16,64}", raw_token):
                token = raw_token
            raw_items = raw.get("items")
            if isinstance(raw_items, list):
                used = set()
                for entry in raw_items:
                    item = _normalize_item(entry)
                    if item is None or item["id"] in used:
                        continue
                    used.add(item["id"])
                    items.append(item)
            if isinstance(raw.get("updated_at"), str) and raw["updated_at"]:
                updated_at = raw["updated_at"]
        self.token = token or secrets.token_hex(16)
        self.items = items
        self.updated_at = updated_at
        if not os.path.exists(self.items_path):
            self._write({"version": 1, "token": self.token, "items": self.items,
                         "updated_at": self.updated_at})

    @staticmethod
    def _replace_atomic(src: str, dest: str) -> None:
        """os.replace 在 Windows 上可能被占用/杀软短暂占用，做几次小重试。"""
        last_error = None
        for attempt in range(6):
            try:
                os.replace(src, dest)
                return
            except PermissionError as exc:  # WinError 5：目标文件被短暂占用
                last_error = exc
                if attempt < 5:
                    time.sleep(0.05 * (attempt + 1))
        raise last_error

    def _write(self, state: dict) -> None:
        directory = os.path.dirname(self.items_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".items-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_atomic(tmp_path, self.items_path)  # 原子替换
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _mutate(self, operation):
        """串行化读改写；失败回滚内存状态。"""
        with self.lock:
            backup = copy.deepcopy(self.items)
            try:
                result = operation(self.items)
                self.updated_at = now_iso()
                self._write({"version": 1, "token": self.token, "items": self.items,
                             "updated_at": self.updated_at})
                return result
            except BaseException:
                self.items = backup
                raise

    # ---- 查询 ----

    def state(self) -> dict:
        with self.lock:
            return {
                "updated_at": self.updated_at,
                "token": self.token,
                "items": [dict(item) for item in self.items],
            }

    def get_item(self, item_id: str) -> dict:
        with self.lock:
            for item in self.items:
                if item["id"] == item_id:
                    return dict(item)
        raise NotFound("资料不存在")

    def get_item_or_none(self, item_id: str):
        try:
            return self.get_item(item_id)
        except NotFound:
            return None

    def file_path(self, item: dict) -> str:
        return os.path.join(self.files_dir, item["id"] + (item.get("ext") or ""))

    # ---- 变更 ----

    @staticmethod
    def _new_id(used) -> str:
        while True:
            item_id = secrets.token_hex(4)
            if item_id not in used:
                return item_id

    def add_item(self, item_id: str, name: str, kind: str, mime: str,
                 size: int, ext: str) -> dict:
        def operation(items):
            if any(entry["id"] == item_id for entry in items):
                raise RuntimeError("条目 id 冲突：%s" % item_id)
            item = {
                "id": item_id,
                "name": name,
                "kind": kind,
                "mime": mime,
                "size": size,
                "ext": ext,
                "description": "",
                "content": "",
                "status": "processing",
                "error": "",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            items.append(item)
            return dict(item)

        return self._mutate(operation)

    def set_result(self, item_id: str, result: dict) -> None:
        """写入 describe_file 的结果。条目已被删除时静默忽略。"""

        def operation(items):
            for item in items:
                if item["id"] == item_id:
                    description = result.get("description") or ""
                    content = result.get("content") or ""
                    error = result.get("error") or ""
                    item["description"] = str(description)[:2000]
                    item["content"] = str(content)
                    item["status"] = "error" if error else "ready"
                    item["error"] = str(error)[:500]
                    kind = result.get("kind")
                    if kind in KINDS:
                        item["kind"] = kind
                    mime = result.get("mime")
                    if isinstance(mime, str) and mime:
                        item["mime"] = mime
                    item["updated_at"] = now_iso()
                    return
            # 条目可能在上传后、描述生成完成前被删除：不视为错误
            log_err("[%s] 描述结果对应的条目已不存在：%s" % (now_iso(), item_id))

        self._mutate(operation)

    def mark_processing(self, item_id: str) -> dict:
        """重新生成描述前重置状态；原始文件保持不变。"""
        def operation(items):
            for item in items:
                if item["id"] == item_id:
                    item["status"] = "processing"
                    item["description"] = ""
                    item["content"] = ""
                    item["error"] = ""
                    item["updated_at"] = now_iso()
                    return dict(item)
            raise NotFound("资料不存在")

        return self._mutate(operation)

    def update_item(self, item_id: str, updates: dict) -> dict:
        def operation(items):
            for item in items:
                if item["id"] == item_id:
                    if "name" in updates:
                        item["name"] = updates["name"]
                        item["kind"] = classify_name(updates["name"])
                        item["mime"] = guess_mime(updates["name"])
                        ext = safe_ext(updates["name"])
                        if ext:
                            item["ext"] = ext
                    if "description" in updates:
                        item["description"] = updates["description"]
                    if "content" in updates:
                        item["content"] = updates["content"]
                    item["updated_at"] = now_iso()
                    return dict(item)
            raise NotFound("资料不存在")

        return self._mutate(operation)

    def delete_item(self, item_id: str) -> dict:
        def operation(items):
            for index, item in enumerate(items):
                if item["id"] == item_id:
                    removed = items.pop(index)
                    return dict(removed)
            raise NotFound("资料不存在")

        return self._mutate(operation)


# ---------------------------------------------------------------- 描述生成 worker

class DescriptionWorker:
    """单线程后台队列：上传后异步生成描述，避免阻塞 HTTP 请求。"""

    def __init__(self, store: ItemStore, describer) -> None:
        self.store = store
        self.describer = describer
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="describe-worker", daemon=True)
        self.thread.start()

    def submit(self, item_id: str, path: str, name: str) -> None:
        self.queue.put((item_id, path, name))

    def _run(self) -> None:
        while True:
            item_id, path, name = self.queue.get()
            try:
                item = self.store.get_item_or_none(item_id)
                if item is None:
                    continue
                result = self.describer(path, name)
                if not isinstance(result, dict):
                    result = {"description": "", "content": "",
                              "error": "描述生成返回了无效结果"}
                self.store.set_result(item_id, result)
                log_out("[%s] 描述生成完成：%s（%s）"
                        % (now_iso(), name, "失败" if result.get("error") else "成功"))
            except Exception:
                log_err("[%s] 描述生成未处理异常：\n%s" % (now_iso(), traceback.format_exc()))
                try:
                    self.store.set_result(item_id, {
                        "description": "《%s》" % name,
                        "content": "",
                        "error": "描述生成失败，请稍后重新上传",
                    })
                except Exception:
                    pass
            finally:
                self.queue.task_done()


def make_default_describer():
    """默认描述器：调用 describe.py；模块缺失/异常时给出兜底结果。"""
    def describer(path: str, name: str) -> dict:
        fallback = {
            "kind": classify_name(name),
            "mime": guess_mime(name),
            "description": "《%s》（%s）" % (name, KIND_LABELS.get(classify_name(name), "其他")),
            "content": "",
            "error": "",
        }
        if describe_module is None:
            fallback["error"] = "描述生成模块 describe.py 缺失"
            return fallback
        try:
            settings = describe_module.Settings.from_env()
            return describe_module.describe_file(path, name, settings)
        except Exception as exc:
            fallback["error"] = "描述生成失败：%s" % exc
            return fallback

    return describer


# ---------------------------------------------------------------- HTTP 处理

def content_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_TYPES:
        return MIME_TYPES[ext]
    mime = mimetypes.guess_type(path)[0]
    return mime or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理：/api/* 与 /s/* 走接口，其余走 web/ 静态文件。"""

    server_version = "SuperContext/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 日志与连接 ----

    def log_message(self, fmt, *args):
        stamp = datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")
        log_err("[%s] %s %s" % (stamp, self.client_address[0], fmt % args))

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(300)  # 大文件上传/下载留足时间
        except OSError:
            pass

    # ---- 方法入口 ----

    def do_GET(self):
        self._guard(self._route_get)

    def do_HEAD(self):
        self._guard(self._route_get)

    def do_POST(self):
        self._guard(self._route_post)

    def do_PATCH(self):
        self._guard(self._route_patch)

    def do_DELETE(self):
        self._guard(self._route_delete)

    def do_PUT(self):
        self._guard(self._route_unsupported)

    def do_OPTIONS(self):
        self._guard(self._route_unsupported)

    # ---- 统一异常 -> JSON 错误响应 ----

    def _guard(self, handler) -> None:
        try:
            handler()
        except RequestTooLarge as exc:
            self._send_json(413, {"error": "请求体过大（单文件上限 %d MB）"
                                           % self.server.max_upload_mb})
            self.close_connection = True
            self._drain_body(exc.length)
        except BadRequest as exc:
            self._send_json(400, {"error": str(exc)})
            self.close_connection = True
        except ValidationError as exc:
            self._send_json(400, {"error": str(exc)})
        except NotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except MethodNotAllowed as exc:
            self._send_json(405, {"error": "方法不允许"}, {"Allow": exc.allow})
            if self.command not in ("GET", "HEAD"):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError as exc:
            log_err("[%s] 请求处理失败：%s" % (now_iso(), exc))
            self._send_json(500, {"error": "服务器内部错误"})
        except Exception:
            log_err("[%s] 未处理异常：\n%s" % (now_iso(), traceback.format_exc()))
            self._send_json(500, {"error": "服务器内部错误"})

    # ---- 路径解析 ----

    def _request_path(self) -> str:
        path = unquote(urlsplit(self.path).path or "/")
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        return path

    @staticmethod
    def _item_id(path: str):
        prefix = "/api/items/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        if rest.endswith("/raw"):
            return None
        if not rest or "/" in rest:
            return None
        return rest

    @staticmethod
    def _raw_item_id(path: str):
        prefix = "/api/items/"
        if not path.startswith(prefix) or not path.endswith("/raw"):
            return None
        item_id = path[len(prefix):-len("/raw")]
        if not item_id or "/" in item_id:
            return None
        return item_id

    @staticmethod
    def _redescribe_id(path: str):
        prefix = "/api/items/"
        suffix = "/redescribe"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        item_id = path[len(prefix):-len(suffix)]
        if not item_id or "/" in item_id:
            return None
        return item_id

    @staticmethod
    def _allowed_methods(path: str):
        """已知路径允许的方法；未知路径返回 None。"""
        if path in ("/healthz", "/api/state"):
            return "GET"
        if path == "/api/items":
            return "POST"
        if Handler._redescribe_id(path) is not None:
            return "POST"
        if Handler._item_id(path) is not None:
            return "PATCH, DELETE"
        if Handler._raw_item_id(path) is not None:
            return "GET"
        if path.startswith("/s/"):
            return "GET"
        return None

    def _reject_wrong_method(self, path: str) -> None:
        """已知路径但方法不对 -> 405；未知路径 -> 404。"""
        allow = self._allowed_methods(path)
        if allow is None:
            raise NotFound("接口不存在")
        raise MethodNotAllowed(allow)

    # ---- GET 路由 ----

    def _route_get(self) -> None:
        path = self._request_path()
        store = self.server.store
        if path == "/healthz":
            self._send_json(200, {"ok": True})
        elif path == "/api/state":
            state = store.state()
            self._send_json(200, {
                "updated_at": state["updated_at"],
                "token": state["token"],
                "max_upload_mb": self.server.max_upload_mb,
                "items": state["items"],
            })
        elif path.startswith("/s/"):
            self._serve_share(path)
        elif self._raw_item_id(path) is not None:
            self._serve_raw(self._raw_item_id(path))
        elif path.startswith("/api"):
            self._reject_wrong_method(path)
        else:
            self._serve_static(path)

    # ---- POST / PATCH / DELETE ----

    def _route_post(self) -> None:
        path = self._request_path()
        if path == "/api/items":
            self._handle_upload()
        elif self._redescribe_id(path) is not None:
            self._handle_redescribe(self._redescribe_id(path))
        else:
            self._reject_wrong_method(path)

    def _route_patch(self) -> None:
        path = self._request_path()
        item_id = self._item_id(path)
        if item_id is None:
            self._reject_wrong_method(path)
        payload = self._read_json_object()
        updates = {}
        if "name" in payload:
            raw_name = payload["name"]
            if not isinstance(raw_name, str):
                raise ValidationError("文件名必须是文本")
            name = sanitize_filename(raw_name)
            if not name:
                raise ValidationError("文件名不能为空")
            updates["name"] = name
        if "description" in payload:
            value = payload["description"]
            if not isinstance(value, str):
                raise ValidationError("描述必须是文本")
            value = value.strip()
            if len(value) > 2000:
                raise ValidationError("描述不能超过 2000 字")
            updates["description"] = value
        if "content" in payload:
            value = payload["content"]
            if not isinstance(value, str):
                raise ValidationError("内容必须是文本")
            if len(value) > 30000:
                raise ValidationError("内容不能超过 30000 字")
            updates["content"] = value
        if not updates:
            self._send_json(200, {"item": self.server.store.get_item(item_id)})
            return
        self._send_json(200, {"item": self.server.store.update_item(item_id, updates)})

    def _route_delete(self) -> None:
        path = self._request_path()
        item_id = self._item_id(path)
        if item_id is None:
            self._reject_wrong_method(path)
        self._read_body(MAX_JSON_BYTES)  # 丢弃请求体，保持连接一致
        removed = self.server.store.delete_item(item_id)
        file_path = self.server.store.file_path(removed)
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_err("[%s] 删除文件失败：%s" % (now_iso(), exc))
        self._send_json(200, {"ok": True})

    def _route_unsupported(self) -> None:
        path = self._request_path()
        if path.startswith("/api") or path.startswith("/s/"):
            self._reject_wrong_method(path)
        else:
            raise MethodNotAllowed("GET")

    # ---- 上传 ----

    def _handle_upload(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        raw_name = (query.get("name") or [""])[0]
        name = sanitize_filename(raw_name)
        if not name:
            raise BadRequest("缺少文件名（请用 ?name=文件名 传参）")

        length = self._content_length(required=True)
        limit = self.server.max_upload_bytes
        if length > limit:
            raise RequestTooLarge(length)
        if length <= 0:
            raise BadRequest("上传内容为空")

        store = self.server.store
        item_id = ItemStore._new_id({entry["id"] for entry in store.items})
        ext = safe_ext(name)
        dest = os.path.join(store.files_dir, item_id + ext)

        written = 0
        try:
            with open(dest, "wb") as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise BadRequest("上传中断，文件不完整")
                    handle.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except BaseException:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise
        if written != length:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise BadRequest("上传中断，文件不完整")

        kind = classify_name(name)
        mime = guess_mime(name)
        try:
            item = store.add_item(item_id, name, kind, mime, written, ext)
        except BaseException:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise
        self.server.worker.submit(item["id"], dest, name)
        log_out("[%s] 收到上传：%s（%s，%s）" % (now_iso(), name, KIND_LABELS.get(kind), fmt_size(written)))
        self._send_json(201, {"item": item})

    def _handle_redescribe(self, item_id: str) -> None:
        """仅重新生成描述，原始文件保持不动。"""
        store = self.server.store
        item = store.get_item(item_id)
        path = store.file_path(item)
        if not os.path.isfile(path):
            raise NotFound("文件不存在")
        item = store.mark_processing(item_id)
        self.server.worker.submit(item_id, path, item["name"])
        log_out("[%s] 重新生成描述：%s" % (now_iso(), item["name"]))
        self._send_json(202, {"item": item})

    # ---- 智能体分享 ----

    def _serve_share(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) < 4 or parts[1] != "s":
            raise NotFound("链接不存在")
        token = parts[2]
        if not hmac.compare_digest(token, self.server.store.token):
            raise NotFound("链接不存在")
        rest = parts[3:]
        if rest == ["index.md"]:
            self._send_bytes(200, self._share_index_markdown().encode("utf-8"),
                             "text/markdown; charset=utf-8")
        elif rest == ["index.json"]:
            body = json.dumps(self._share_index_json(), ensure_ascii=False, indent=2)
            self._send_bytes(200, body.encode("utf-8"), "application/json; charset=utf-8")
        elif len(rest) == 2 and rest[0] == "items" and rest[1].endswith(".md"):
            item_id = rest[1][:-3]
            item = self.server.store.get_item(item_id)
            self._send_bytes(200, self._share_item_markdown(item).encode("utf-8"),
                             "text/markdown; charset=utf-8")
        elif len(rest) == 2 and rest[0] == "files":
            self._serve_raw(rest[1])
        else:
            raise NotFound("链接不存在")

    def _public_base(self) -> str:
        return self.server.public_prefix + "/s/" + self.server.store.token

    def _share_index_markdown(self) -> str:
        state = self.server.store.state()
        items = list(reversed(state["items"]))  # 最新在前
        base = self._public_base()
        lines = [
            "# 超级上下文 · 资料清单",
            "",
            "> 共 %d 份资料 · 更新于 %s" % (len(items), fmt_time(state["updated_at"])),
            "> 使用方式：阅读每份资料的「描述」判断是否相关；需要素材本身时 GET 该条的「原始文件」——",
            "> 返回上传时的原始字节（图片 / 音频 / 视频保持原样，Content-Type 正确，不会转成文本），",
            "> 智能体可直接下载后用多模态模型处理。文字信息（文档摘要、音频转写、视频画面概述）见「详情链接」。",
            "> 链接已含访问令牌，直接请求即可，无需账号密码。",
        ]
        for index, item in enumerate(items, 1):
            label = KIND_LABELS.get(item["kind"], "其他")
            description = item["description"].strip()
            if not description:
                if item["status"] == "processing":
                    description = "（描述生成中…）"
                elif item["status"] == "error":
                    description = "（描述生成失败：%s）" % (item["error"] or "未知原因")
                else:
                    description = "（无描述）"
            elif item["status"] == "error":
                description += "（描述生成失败：%s）" % (item["error"] or "未知原因")
            lines.extend([
                "",
                "## %d. %s（%s）" % (index, item["name"], label),
                "- 原始文件：%s/files/%s" % (base, item["id"]),
                "- 详情链接：%s/items/%s.md" % (base, item["id"]),
                "- 类型：%s · MIME：%s · 大小：%s · 上传：%s"
                % (label, item["mime"] or "未知",
                   fmt_size(item["size"]), fmt_time(item["created_at"])),
                "- 描述：%s" % description,
            ])
            if item["kind"] == "image":
                # 支持渲染 Markdown 图片的智能体可直接看到原图
                lines.append("- 图片：![%s](%s/files/%s)"
                             % (md_alt(item["name"]), base, item["id"]))
        return "\n".join(lines) + "\n"

    def _share_index_json(self) -> dict:
        state = self.server.store.state()
        base = self._public_base()
        items = []
        for item in reversed(state["items"]):
            items.append({
                "id": item["id"],
                "name": item["name"],
                "kind": item["kind"],
                "mime": item["mime"],
                "size": item["size"],
                "status": item["status"],
                "description": item["description"],
                "error": item["error"],
                "created_at": item["created_at"],
                "files_url": "%s/files/%s" % (base, item["id"]),
                "original_url": "%s/files/%s" % (base, item["id"]),
                "detail_url": "%s/items/%s.md" % (base, item["id"]),
            })
        return {
            "updated_at": state["updated_at"],
            "count": len(items),
            "items": items,
        }

    def _share_item_markdown(self, item: dict) -> str:
        base = self._public_base()
        label = KIND_LABELS.get(item["kind"], "其他")
        description = item["description"].strip() or "（无描述）"
        content = item["content"].strip()
        raw_url = "%s/files/%s" % (base, item["id"])
        lines = [
            "# %s" % item["name"],
            "",
            "- 类型：%s · MIME：%s" % (label, item["mime"] or "未知"),
            "- 大小：%s" % fmt_size(item["size"]),
            "- 上传：%s" % fmt_time(item["created_at"]),
            "",
            "## 原始文件",
            "",
            "- 链接：%s" % raw_url,
            "- 说明：返回上传时的原始字节（未转文本），可直接下载或交给多模态模型处理。",
        ]
        if item["kind"] == "image":
            lines.extend(["", "![%s](%s)" % (md_alt(item["name"]), raw_url)])
        lines.extend(["", "## 描述", "", description])
        if item["status"] == "error":
            lines.extend(["", "> 描述生成失败：%s" % (item["error"] or "未知原因")])
        if content:
            lines.extend(["", "## 内容（辅助文本，可选）", "", content])
        elif item["kind"] == "image":
            lines.extend(["", "## 内容（辅助文本，可选）", "",
                          "（图片没有辅助文本：请直接使用上面的「原始文件」，把图片交给模型处理。）"])
        else:
            lines.extend(["", "## 内容（辅助文本，可选）", "", "（无辅助文本内容）"])
        return "\n".join(lines) + "\n"

    def _serve_raw(self, item_id: str) -> None:
        item = self.server.store.get_item(item_id)
        path = self.server.store.file_path(item)
        if not os.path.isfile(path):
            raise NotFound("文件不存在")
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            raise NotFound("文件不存在")
        self._send_bytes(200, body, item.get("mime") or content_type_for(path),
                         cache_control="private, max-age=0, must-revalidate",
                         disposition=self._disposition(item["name"]))

    # ---- 请求体 ----

    def _content_length(self, required: bool = False) -> int:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not str(raw_length).strip():
            if required:
                raise BadRequest("缺少 Content-Length")
            return 0
        try:
            length = int(raw_length)
        except ValueError:
            raise BadRequest("Content-Length 不合法")
        if length < 0:
            raise BadRequest("Content-Length 不合法")
        return length

    def _read_body(self, limit: int = MAX_JSON_BYTES) -> bytes:
        length = self._content_length()
        if length > limit:
            raise RequestTooLarge(length)
        if length == 0:
            return b""
        data = self.rfile.read(length)
        if len(data) != length:
            raise BadRequest("请求体读取不完整")
        return data

    def _read_json_object(self) -> dict:
        raw = self._read_body()
        if not raw:
            raise BadRequest("请求体必须是 JSON 对象")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("请求体必须是合法 JSON")
        if not isinstance(payload, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return payload

    def _drain_body(self, length: int) -> None:
        remaining = min(length, MAX_DRAIN_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    # ---- 静态文件 ----

    def _serve_static(self, path: str) -> None:
        web_dir = self.server.web_dir
        relative = path.lstrip("/")
        if relative == "":
            relative = "index.html"
        parts = [part for part in relative.split("/") if part]
        if any(part in ("..", ".") for part in parts):
            raise NotFound("页面不存在")
        for part in parts:
            if "\\" in part or ":" in part or "\x00" in part:
                raise NotFound("页面不存在")
        root = os.path.realpath(web_dir)
        target = os.path.realpath(os.path.join(root, *parts))
        if target != root and not target.startswith(root + os.sep):
            raise NotFound("页面不存在")  # 防目录穿越
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            raise NotFound("页面不存在")
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            raise NotFound("页面不存在")
        self._send_bytes(200, body, content_type_for(target), cache_control="no-cache")

    # ---- 响应 ----

    @staticmethod
    def _disposition(name: str) -> str:
        ascii_name = re.sub(r'[^\x20-\x7e]', "_", name).replace('"', "") or "file"
        return "inline; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_name.replace(";", "_"), quote(name, safe=""))

    def _send_json(self, status: int, payload: dict, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    extra_headers=None, cache_control: str = "no-store",
                    disposition: str | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True


class ContextServer(ThreadingHTTPServer):
    """多线程 HTTP 服务，持有存储、描述 worker、静态目录与对外前缀。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: ItemStore, worker: DescriptionWorker,
                 web_dir: str, public_prefix: str, max_upload_mb: int) -> None:
        super().__init__(address, Handler)
        self.store = store
        self.worker = worker
        self.web_dir = os.path.abspath(web_dir)
        self.public_prefix = public_prefix.rstrip("/")
        self.max_upload_mb = int(max_upload_mb)
        self.max_upload_bytes = int(max_upload_mb) * 1024 * 1024


# ---------------------------------------------------------------- 启动

def create_server(host=None, port=None, data_dir=None, web_dir=None,
                  describer=None, public_prefix=None, max_upload_mb=None):
    """创建服务实例（测试也走这里：port=0 自动分配，describer 可注入）。"""
    host = host or os.environ.get("HOST") or DEFAULT_HOST
    if port is None:
        env_port = os.environ.get("PORT", "").strip()
        port = int(env_port) if env_port else DEFAULT_PORT
    data_dir = data_dir or os.environ.get("CONTEXT_DATA") or os.path.join(BASE_DIR, "data")
    web_dir = web_dir or os.environ.get("CONTEXT_WEB") or os.path.join(BASE_DIR, "web")
    if max_upload_mb is None:
        env_limit = os.environ.get("CONTEXT_MAX_UPLOAD_MB", "").strip()
        max_upload_mb = int(env_limit) if env_limit.isdigit() else DEFAULT_MAX_UPLOAD_MB
    store = ItemStore(data_dir)
    worker = DescriptionWorker(store, describer or make_default_describer())
    if not public_prefix:
        public_prefix = os.environ.get("CONTEXT_PUBLIC_PREFIX") or (
            "http://127.0.0.1:%d" % int(port))
    return ContextServer((host, int(port)), store, worker, web_dir,
                         public_prefix, max_upload_mb)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    port = None
    if args:
        try:
            port = int(args[0])
        except ValueError:
            print("用法: python3 server.py [port]", file=sys.stderr)
            return 2
    try:
        httpd = create_server(port=port)
    except (OSError, ValueError) as exc:
        log_err("[%s] 服务启动失败：%s" % (now_iso(), exc))
        return 1
    host, real_port = httpd.server_address[0], httpd.server_address[1]
    display_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    log_out("[%s] 超级上下文服务已启动: http://%s:%d" % (now_iso(), display_host, real_port))
    log_out("[%s] 数据目录: %s" % (now_iso(), httpd.store.data_dir))
    log_out("[%s] 静态目录: %s" % (now_iso(), httpd.web_dir))
    log_out("[%s] 对外前缀: %s" % (now_iso(), httpd.public_prefix))
    log_out("[%s] 单文件上限: %d MB" % (now_iso(), httpd.max_upload_mb))
    log_out("[%s] 智能体链接: %s/s/%s/index.md"
            % (now_iso(), httpd.public_prefix, httpd.store.token))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log_out("[%s] 收到中断，正在停止…" % now_iso())
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
