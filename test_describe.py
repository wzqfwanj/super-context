#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""describe.py 离线测试（不访问网络、不需要 ffmpeg）。

运行：
    python test_describe.py

真实的模型调用/音视频处理在 dev 主机上做过端到端验收（见 README「验收记录」），
这里覆盖分类、文本提取、兜底与异常路径，保证 CI/本地可重复。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile

import describe


DOCX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>第一段：超级上下文测试文档</w:t></w:r></w:p>
    <w:p><w:r><w:t>第二段：</w:t></w:r><w:r><w:t>图片、文档、音视频</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

PPTX_SLIDE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>第一页标题</a:t></a:r></a:p>
    <a:p><a:r><a:t>要点内容</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>
"""

XLSX_SHARED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>姓名</t></si><si><t>年龄</t></si><si><t>张三</t></si><si><t>30</t></si>
</sst>
"""

XLSX_SHEET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>30</v></c></row>
  </sheetData>
</worksheet>
"""


class ClassifyTest(unittest.TestCase):

    def test_known_kinds(self):
        cases = {
            "a.png": "image",
            "b.JPG": "image",
            "c.svg": "image",
            "d.md": "document",
            "e.docx": "document",
            "f.pdf": "document",
            "g.xlsx": "document",
            "h.mp3": "audio",
            "i.wav": "audio",
            "j.mp4": "video",
            "k.mov": "video",
            "l.xyz": "other",
            "没有扩展名": "other",
            "": "other",
        }
        for name, expected in cases.items():
            self.assertEqual(describe.classify(name), expected, name)

    def test_guess_mime(self):
        self.assertEqual(describe.guess_mime("a.png"), "image/png")
        self.assertEqual(describe.guess_mime("a.txt"), "text/plain")
        self.assertEqual(describe.guess_mime("a.zzz"), "application/octet-stream")


class ExtractionTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="describe-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, name, content):
        path = os.path.join(self.dir, name)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode, **({} if isinstance(content, bytes) else {"encoding": "utf-8"})) as handle:
            handle.write(content)
        return path

    def make_docx(self):
        path = os.path.join(self.dir, "test.docx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", DOCX_XML)
        return path

    def make_pptx(self):
        path = os.path.join(self.dir, "test.pptx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", PPTX_SLIDE_XML)
        return path

    def make_xlsx(self):
        path = os.path.join(self.dir, "test.xlsx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", XLSX_SHARED_XML)
            archive.writestr("xl/worksheets/sheet1.xml", XLSX_SHEET_XML)
        return path

    def test_plain_utf8_and_gbk(self):
        utf8 = self.make("utf8.txt", "中文 UTF-8 文本")
        self.assertIn("中文", describe._extract_plain(utf8, 30000))
        gbk = self.make("gbk.txt", "中文 GBK 文本".encode("gbk"))
        self.assertIn("中文", describe._extract_plain(gbk, 30000))

    def test_decode_text(self):
        text, encoding = describe._decode_text("你好".encode("utf-8"))
        self.assertEqual((text, encoding), ("你好", "utf-8-sig"))
        text, encoding = describe._decode_text("你好".encode("gbk"))
        self.assertIn(encoding, ("gbk", "gb18030"))
        self.assertEqual(text, "你好")

    def test_binary_is_rejected_as_plain_text(self):
        path = self.make("bin.dat", b"\x00\x01\x02\x03" * 100)
        with self.assertRaises(describe._ModelError):
            describe._extract_plain(path, 30000)

    def test_docx_extraction(self):
        text = describe._extract_docx(self.make_docx(), 30000)
        self.assertIn("第一段：超级上下文测试文档", text)
        self.assertIn("第二段：图片、文档、音视频", text)
        self.assertEqual(text.count("\n"), 1)  # 两个段落两行

    def test_pptx_extraction(self):
        text = describe._extract_pptx(self.make_pptx(), 30000)
        self.assertIn("【第1页】", text)
        self.assertIn("第一页标题", text)
        self.assertIn("要点内容", text)

    def test_xlsx_extraction(self):
        text = describe._extract_xlsx(self.make_xlsx(), 30000)
        self.assertIn("姓名", text)
        self.assertIn("张三", text)
        self.assertIn("30", text)

    def test_broken_zip_is_readable_error(self):
        path = self.make("broken.docx", b"not a zip")
        with self.assertRaises(describe._ModelError):
            describe._extract_docx(path, 30000)


class DescribeFileFallbackTest(unittest.TestCase):
    """没有 API Key 时 describe_file 也应给出兜底描述且不抛异常。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="describe-fallback-")
        self.settings = describe.Settings(api_key="")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_no_key_fallback(self):
        path = self.make("说明.md", "这是正文".encode("utf-8"))
        result = describe.describe_file(path, "说明.md", self.settings)
        self.assertEqual(result["kind"], "document")
        self.assertIn("未配置百炼 API Key", result["error"])
        self.assertIn("说明.md", result["description"])
        self.assertEqual(result["content"], "")

    def test_empty_file(self):
        path = self.make("empty.txt", b"")
        result = describe.describe_file(path, "empty.txt", self.settings)
        self.assertIn("文件为空", result["error"])
        self.assertIn("empty.txt", result["description"])

    def test_missing_file_does_not_raise(self):
        result = describe.describe_file(
            os.path.join(self.dir, "nope.txt"), "nope.txt", self.settings)
        self.assertTrue(result["error"])
        self.assertIn("nope.txt", result["description"])

    def test_unknown_type_keeps_text_content(self):
        path = self.make("data.xyz", "纯文本内容 123".encode("utf-8"))
        # 其他类型只做文本解码，不调用模型；给一个假 key 让流程走到 handler
        result = describe.describe_file(
            path, "data.xyz", describe.Settings(api_key="dummy-for-offline-test"))
        self.assertEqual(result["kind"], "other")
        self.assertIn("纯文本内容", result["content"])
        self.assertEqual(result["error"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
