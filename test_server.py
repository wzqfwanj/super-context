#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「超级上下文」服务端接口测试（标准库 unittest）。

运行：
    python test_server.py
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

import server as app


class FakeDescriber:
    """测试用假描述器：延迟返回固定结构的描述。"""

    def __init__(self, delay: float = 0.25, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.calls = []

    def __call__(self, path: str, name: str) -> dict:
        self.calls.append(name)
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("模拟模型失败")
        kind = app.classify_name(name)
        return {
            "kind": kind,
            "mime": app.guess_mime(name),
            "description": "描述：%s" % name,
            "content": "正文：%s" % name,
            "error": "",
        }


class ServerTestCase(unittest.TestCase):
    """公共夹具：起一个真实 HTTP 服务（随机端口 + 临时数据目录）。"""

    describer = None
    max_upload_mb = 1

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="super-context-test-")
        cls.web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        cls.describer = cls.describer or FakeDescriber()
        cls.httpd = app.create_server(
            host="127.0.0.1",
            port=0,
            data_dir=cls.data_dir,
            web_dir=cls.web_dir,
            describer=cls.describer,
            public_prefix="https://example.test/context",
            max_upload_mb=cls.max_upload_mb,
        )
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    # ---- HTTP 工具 ----

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            return response, data
        finally:
            conn.close()

    def json_request(self, method: str, path: str, payload=None, headers=None):
        body = None
        hdrs = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        response, data = self.request(method, path, body, hdrs)
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            decoded = None
        return response, decoded, data

    def upload(self, name: str, content: bytes, extra_headers=None):
        headers = {"Content-Type": "application/octet-stream"}
        headers.update(extra_headers or {})
        response, data = self.request("POST", "/api/items?name=" + quote(name), content, headers)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        return response, payload, data

    def state(self) -> dict:
        _, data, _ = self.json_request("GET", "/api/state")
        return data

    def wait_status(self, item_id: str, expected=("ready", "error"), timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            for item in state["items"]:
                if item["id"] == item_id and item["status"] in expected:
                    return item
            time.sleep(0.05)
        self.fail("等待条目 %s 状态超时；当前状态：%s"
                  % (item_id, [item["status"] for item in self.state()["items"]]))

    def item_path(self, item: dict) -> str:
        return os.path.join(self.data_dir, "files", item["id"] + item.get("ext", ""))

    def all_items(self):
        return self.state()["items"]


class HealthAndStateTest(ServerTestCase):

    def test_healthz(self):
        response, payload, _ = self.json_request("GET", "/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"ok": True})

    def test_state_initial(self):
        response, payload, _ = self.json_request("GET", "/api/state")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["max_upload_mb"], 1)
        self.assertRegex(payload["token"], r"^[0-9a-f]{32}$")

    def test_unknown_api_is_404(self):
        response, payload, _ = self.json_request("GET", "/api/nope")
        self.assertEqual(response.status, 404)
        self.assertIn("error", payload)


class UploadTest(ServerTestCase):

    def test_upload_then_ready(self):
        response, payload, _ = self.upload("说明.md", "# 你好\n这是资料".encode("utf-8"))
        self.assertEqual(response.status, 201)
        item = payload["item"]
        self.assertEqual(item["name"], "说明.md")
        self.assertEqual(item["kind"], "document")
        self.assertEqual(item["status"], "processing")
        self.assertTrue(os.path.isfile(self.item_path(item)))

        ready = self.wait_status(item["id"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["description"], "描述：说明.md")
        self.assertEqual(ready["content"], "正文：说明.md")

    def test_upload_sanitizes_path(self):
        response, payload, _ = self.upload("..\\..\\evil.txt", b"x")
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["item"]["name"], "evil.txt")
        files = os.listdir(os.path.join(self.data_dir, "files"))
        self.assertIn(payload["item"]["id"] + ".txt", files)

    def test_upload_missing_name(self):
        response, data = self.request("POST", "/api/items", b"x",
                                      {"Content-Type": "application/octet-stream"})
        payload = data
        self.assertEqual(response.status, 400)
        self.assertIn("文件名", json.loads(payload.decode("utf-8"))["error"])

    def test_upload_empty_body(self):
        response, payload, _ = self.upload("空.txt", b"")
        self.assertEqual(response.status, 400)

    def test_upload_too_large(self):
        big = b"x" * (1 * 1024 * 1024 + 1024)  # 超过 1MB 限制
        response, payload, _ = self.upload("big.bin", big)
        self.assertEqual(response.status, 413)
        self.assertIn("过大", payload["error"])
        # 请求体已被丢弃，服务仍可用
        response, _, _ = self.json_request("GET", "/healthz")
        self.assertEqual(response.status, 200)

    def test_upload_kind_detection(self):
        cases = {
            "a.png": "image",
            "b.mp3": "audio",
            "c.mp4": "video",
            "d.pdf": "document",
            "e.xyz": "other",
        }
        for name, kind in cases.items():
            response, payload, _ = self.upload(name, b"data")
            self.assertEqual(response.status, 201)
            self.assertEqual(payload["item"]["kind"], kind, name)
            self.wait_status(payload["item"]["id"])

    def test_upload_extensionless_image_is_sniffed(self):
        """微信保存的图片常是一长串无扩展名文件名，应按文件头识别为图片。"""
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        name = "_cgi-bin_mmwebwx-bin_webwxgetmsgimg__&MsgID=1&skey=@crypt_abc&mm"
        response, payload, _ = self.upload(name, png)
        self.assertEqual(response.status, 201)
        item = payload["item"]
        self.assertEqual(item["kind"], "image")
        self.assertEqual(item["mime"], "image/png")
        self.assertTrue(item["name"].endswith(".png"), item["name"])
        self.assertIn(item["id"] + ".png", os.listdir(os.path.join(self.data_dir, "files")))
        ready = self.wait_status(item["id"])
        self.assertEqual(ready["status"], "ready")
        _, raw = self.request("GET", "/api/items/%s/raw" % item["id"])
        self.assertEqual(raw, png)

    def test_upload_unknown_extension_uses_sniffed_type(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        response, payload, _ = self.upload("相片.weird", jpeg)
        self.assertEqual(response.status, 201)
        item = payload["item"]
        self.assertEqual(item["kind"], "image")
        self.assertEqual(item["mime"], "image/jpeg")
        self.assertEqual(item["name"], "相片.jpg")
        self.wait_status(item["id"])

    def test_upload_chinese_name_header(self):
        response, payload, _ = self.upload("中文 名称.txt", b"hi")
        self.assertEqual(response.status, 201)
        item = self.wait_status(payload["item"]["id"])
        response, data = self.request("GET", "/api/items/%s/raw" % item["id"])
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"hi")
        disposition = response.getheader("Content-Disposition", "")
        self.assertIn("filename*=UTF-8''", disposition)


class PatchDeleteTest(ServerTestCase):

    def test_patch_description_and_content(self):
        _, payload, _ = self.upload("a.txt", b"hello")
        item = self.wait_status(payload["item"]["id"])
        response, updated, _ = self.json_request(
            "PATCH", "/api/items/%s" % item["id"],
            {"description": "新描述", "content": "新内容"})
        self.assertEqual(response.status, 200)
        self.assertEqual(updated["item"]["description"], "新描述")
        self.assertEqual(updated["item"]["content"], "新内容")
        refreshed = next(entry for entry in self.state()["items"] if entry["id"] == item["id"])
        self.assertEqual(refreshed["description"], "新描述")

    def test_patch_empty_is_noop(self):
        _, payload, _ = self.upload("a.txt", b"hello")
        item = self.wait_status(payload["item"]["id"])
        response, updated, _ = self.json_request("PATCH", "/api/items/%s" % item["id"], {})
        self.assertEqual(response.status, 200)
        self.assertEqual(updated["item"]["description"], "描述：a.txt")

    def test_patch_invalid_types(self):
        _, payload, _ = self.upload("a.txt", b"hello")
        item = self.wait_status(payload["item"]["id"])
        response, body, _ = self.json_request("PATCH", "/api/items/%s" % item["id"],
                                              {"description": 123})
        self.assertEqual(response.status, 400)
        self.assertIn("描述", body["error"])

    def test_patch_unknown_item(self):
        response, body, _ = self.json_request("PATCH", "/api/items/deadbeef",
                                              {"description": "x"})
        self.assertEqual(response.status, 404)

    def test_delete(self):
        _, payload, _ = self.upload("del.txt", b"bye")
        item = self.wait_status(payload["item"]["id"])
        path = self.item_path(item)
        self.assertTrue(os.path.exists(path))
        response, body, _ = self.json_request("DELETE", "/api/items/%s" % item["id"])
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertFalse(os.path.exists(path))
        response, _ = self.request("GET", "/api/items/%s/raw" % item["id"])
        self.assertEqual(response.status, 404)
        response, _, _ = self.json_request("DELETE", "/api/items/%s" % item["id"])
        self.assertEqual(response.status, 404)


class ShareTest(ServerTestCase):
    def test_share_index_markdown(self):
        _, payload, _ = self.upload("图.png", b"pngdata")
        item = self.wait_status(payload["item"]["id"])
        token = self.state()["token"]
        response, data = self.request("GET", "/s/%s/index.md" % token)
        self.assertEqual(response.status, 200)
        self.assertIn("text/markdown", response.getheader("Content-Type"))
        text = data.decode("utf-8")
        self.assertIn("图.png", text)
        self.assertIn("描述：图.png", text)
        self.assertIn("- 原始文件：", text)
        self.assertIn("MIME：image/png", text)
        self.assertIn("https://example.test/context/s/%s/files/%s" % (token, item["id"]), text)
        self.assertIn("https://example.test/context/s/%s/items/%s.md" % (token, item["id"]), text)
        # 图片在清单里带 Markdown 图片直链，支持渲染图片的智能体可直接取原图
        self.assertIn("![图.png](https://example.test/context/s/%s/files/%s)"
                      % (token, item["id"]), text)

    def test_share_index_json(self):
        _, payload, _ = self.upload("j.txt", b"j")
        self.wait_status(payload["item"]["id"])
        token = self.state()["token"]
        response, data = self.request("GET", "/s/%s/index.json" % token)
        self.assertEqual(response.status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(body["count"], len(self.all_items()))
        self.assertTrue(any(entry["name"] == "j.txt" for entry in body["items"]))

    def test_share_item_detail(self):
        _, payload, _ = self.upload("详情.md", b"detail")
        item = self.wait_status(payload["item"]["id"])
        token = self.state()["token"]
        response, data = self.request("GET", "/s/%s/items/%s.md" % (token, item["id"]))
        self.assertEqual(response.status, 200)
        text = data.decode("utf-8")
        self.assertIn("# 详情.md", text)
        self.assertIn("## 描述", text)
        self.assertIn("正文：详情.md", text)
        self.assertIn("/files/%s" % item["id"], text)

    def test_share_file_download(self):
        content = b"raw-bytes-\x00\x01\x02"
        _, payload, _ = self.upload("raw.bin", content)
        item = self.wait_status(payload["item"]["id"])
        token = self.state()["token"]
        response, data = self.request("GET", "/s/%s/files/%s" % (token, item["id"]))
        self.assertEqual(response.status, 200)
        self.assertEqual(data, content)
        self.assertIn("filename*=", response.getheader("Content-Disposition", ""))

    def test_share_wrong_token_404(self):
        response, data = self.request("GET", "/s/deadbeefdeadbeefdeadbeefdeadbeef/index.md")
        self.assertEqual(response.status, 404)
        self.assertIn("error", json.loads(data.decode("utf-8")))

    def test_share_unknown_file_404(self):
        token = self.state()["token"]
        response, _ = self.request("GET", "/s/%s/files/notexist" % token)
        self.assertEqual(response.status, 404)
        response, _ = self.request("GET", "/s/%s/nope" % token)
        self.assertEqual(response.status, 404)


class RedescribeTest(ServerTestCase):
    """重新生成描述：只更新描述，原始文件字节不变。"""

    def test_redescribe_regenerates_description(self):
        _, payload, _ = self.upload("r.txt", b"hello")
        item = self.wait_status(payload["item"]["id"])
        self.json_request("PATCH", "/api/items/%s" % item["id"],
                          {"description": "旧描述"})
        response, payload, _ = self.json_request(
            "POST", "/api/items/%s/redescribe" % item["id"])
        self.assertEqual(response.status, 202)
        self.assertEqual(payload["item"]["status"], "processing")
        regenerated = self.wait_status(item["id"])
        self.assertEqual(regenerated["description"], "描述：r.txt")
        _, raw = self.request("GET", "/api/items/%s/raw" % item["id"])
        self.assertEqual(raw, b"hello")

    def test_redescribe_unknown_item(self):
        response, body, _ = self.json_request("POST", "/api/items/deadbeef/redescribe")
        self.assertEqual(response.status, 404)

    def test_redescribe_wrong_method(self):
        response, _, _ = self.json_request("GET", "/api/items/deadbeef/redescribe")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")


class StaticAndMethodTest(ServerTestCase):

    def test_index_page(self):
        response, data = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        with open(os.path.join(self.web_dir, "index.html"), "rb") as handle:
            self.assertEqual(data, handle.read())

    def test_static_css(self):
        response, data = self.request("GET", "/style.css")
        self.assertEqual(response.status, 200)
        self.assertIn("text/css", response.getheader("Content-Type"))

    def test_traversal_blocked(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/..%2fserver.py"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 404, path)

    def test_unknown_static_404(self):
        response, _ = self.request("GET", "/missing.html")
        self.assertEqual(response.status, 404)

    def test_wrong_methods(self):
        cases = [
            ("PUT", "/api/items", "POST"),
            ("POST", "/api/state", "GET"),
            ("GET", "/api/items", "POST"),
            ("POST", "/s/token/index.md", "GET"),
        ]
        token = self.state()["token"]
        for method, path, allow in cases:
            if path.startswith("/s/token"):
                path = "/s/%s/index.md" % token
            response, _, _ = self.json_request(method, path)
            self.assertEqual(response.status, 405, (method, path))
            self.assertIn(allow, response.getheader("Allow", ""))

    def test_head_request(self):
        response, data = self.request("HEAD", "/")
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"")
        self.assertGreater(int(response.getheader("Content-Length")), 0)


class FailureDescriberTest(ServerTestCase):
    """描述器抛异常时：上传成功、状态 error、描述兜底。"""

    describer = FakeDescriber(delay=0.0, fail=True)

    def test_failure_marks_error(self):
        response, payload, _ = self.upload("bad.txt", b"x")
        self.assertEqual(response.status, 201)
        item = self.wait_status(payload["item"]["id"], expected=("error",))
        self.assertEqual(item["status"], "error")
        self.assertIn("描述生成失败", item["error"])
        token = self.state()["token"]
        _, data = self.request("GET", "/s/%s/index.md" % token)
        self.assertIn("描述生成失败", data.decode("utf-8"))


class StorePersistenceTest(unittest.TestCase):
    """直接测 ItemStore 的持久化与重启修复。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="super-context-store-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_token_persists(self):
        first = app.ItemStore(self.dir)
        second = app.ItemStore(self.dir)
        self.assertEqual(first.token, second.token)
        self.assertRegex(first.token, r"^[0-9a-f]{32}$")

    def test_processing_item_recovered_as_error(self):
        store = app.ItemStore(self.dir)
        token = store.token
        raw = {
            "version": 1,
            "token": token,
            "items": [{
                "id": "abcd1234",
                "name": "重启.txt",
                "kind": "document",
                "mime": "text/plain",
                "size": 3,
                "ext": ".txt",
                "description": "",
                "content": "",
                "status": "processing",
                "error": "",
                "created_at": app.now_iso(),
                "updated_at": app.now_iso(),
            }],
            "updated_at": app.now_iso(),
        }
        with open(os.path.join(self.dir, "items.json"), "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False)
        reloaded = app.ItemStore(self.dir)
        item = reloaded.state()["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("重新上传", item["error"])

    def test_corrupt_items_file_backed_up(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "items.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        store = app.ItemStore(self.dir)
        self.assertEqual(store.state()["items"], [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "items.json.corrupt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
