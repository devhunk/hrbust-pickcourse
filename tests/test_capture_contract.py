"""在本机回放真实抓包并校验 wire 请求；不连接学校、不使用抓包 Cookie。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import zlib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from course_helper import HttpResult, JwzxClient, OpenState, Settings, run_selected
from selection_workflow import Course, SelectionError, SelectionSessionError, SelectionWorkflow


ROOT = Path(__file__).resolve().parents[1]
FIXTURES_CAPTURES = ROOT / "tests" / "fixtures" / "captures"
LEGACY_CAPTURES = ROOT / "origin" / "选课全流程"
CAPTURES = FIXTURES_CAPTURES if FIXTURES_CAPTURES.exists() else LEGACY_CAPTURES


def capture(number, kind, updated=False):
    folder = CAPTURES / "更新" if updated else CAPTURES
    target_file = folder / f"[{number}] {kind}_jwzx.hrbust.edu.cn_message.txt"
    if not target_file.exists():
        raise FileNotFoundError(f"Fixture file not found: {target_file}")
    raw = target_file.read_bytes()
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").splitlines()
    headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    return lines[0], headers, body


class CaptureContractTests(unittest.TestCase):
    def setUp(self):
        if not CAPTURES.exists():
            raise unittest.SkipTest("Capture fixtures not found")
        self.temp = tempfile.TemporaryDirectory()
        self.steps = [(102, False), (103, False), (117, False), (119, False),
                      (121, False), (11, True), (12, True), (17, True),
                      (20, True), (24, True), (35, True), (36, True)]
        self.index = 0
        self.errors = []
        self.visited = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.serve_capture()

            def do_POST(self):
                self.serve_capture()

            def serve_capture(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.visited.append((self.command, urlsplit(self.path).path))
                try:
                    if owner.index >= len(owner.steps):
                        raise AssertionError("Unexpected extra request: " + urlsplit(self.path).path)
                    number, updated = owner.steps[owner.index]
                    line, expected_headers, expected_body = capture(number, "request", updated)
                    method, target, _ = line.split(" ")
                    owner.assertEqual(method, self.command)
                    owner.assertEqual(urlsplit(target).path, urlsplit(self.path).path)
                    expected_query = parse_qsl(urlsplit(target).query, keep_blank_values=True)
                    actual_query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
                    if number == 24 and updated:
                        owner.assertRegex(urlsplit(self.path).query, r"^0\.\d+$")
                    else:
                        owner.assertEqual(
                            [(k, v) for k, v in expected_query if k != "_"],
                            [(k, v) for k, v in actual_query if k != "_"],
                        )
                        if any(k == "_" for k, _ in expected_query):
                            nonce = [v for k, v in actual_query if k == "_"]
                            owner.assertEqual(1, len(nonce))
                            owner.assertTrue(nonce[0].isdigit())
                    for name, value in expected_headers.items():
                        if name.lower() in {"host", "cookie", "connection"}:
                            continue
                        value = value.replace("http://jwzx.hrbust.edu.cn", owner.base)
                        owner.assertEqual(value, self.headers.get(name), name)
                    owner.assertEqual("JSESSIONID=replay-session", self.headers.get("Cookie"))
                    owner.assertEqual(expected_body.rstrip(b"\r\n"), body)
                    owner.index += 1
                    line, headers, response_body = capture(number, "response", updated)
                    self.send_response(int(line.split()[1]))
                    # 抓包的 Transfer-Encoding 已被抓包软件解码；重新发送正确长度。
                    for name in ("Content-Type", "Location"):
                        if name in headers:
                            self.send_header(name, headers[name].replace("http://jwzx.hrbust.edu.cn", owner.base))
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                except AssertionError as error:
                    owner.errors.append(str(error))
                    self.send_response(409)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.settings = replace(
            Settings.load(Path(self.temp.name) / "absent.json"),
            base_url=self.base,
            referer=self.base + "/academic/index_new.jsp",
            cookie_file=Path(self.temp.name) / "session.txt",
            captcha_file=Path(self.temp.name) / "captcha.jpg",
            session_env="PICKCOURSE_REPLAY_SESSION",
            timeout_seconds=2,
        )
        with patch.dict(os.environ, {"PICKCOURSE_REPLAY_SESSION": "replay-session"}):
            self.client = JwzxClient(self.settings)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.assertEqual([], self.errors)

    def test_actual_capture_navigation_headers_parameters_and_submit(self):
        workflow = SelectionWorkflow(self.client)
        workflow.open_page()
        self.assertEqual(0, workflow.status()["status"])
        courses = workflow.list_courses()
        target = next(c for c in courses if c.epid == "234838946")
        self.assertEqual("匹克球2班", target.alias)
        _, _, body = capture(20, "request", True)
        epids = [v for k, v in parse_qsl(body.decode()) if k == "epid"]
        capacity = workflow.capacities(epids)[target.epid]
        self.assertEqual((9, 38), (capacity.remaining, capacity.total))
        self.assertTrue(workflow.fetch_captcha().read_bytes().startswith(b"\xff\xd8"))
        self.assertTrue(workflow.validate_captcha("277633"))
        before = len(self.visited)
        result = workflow.submit_once(target)
        self.assertTrue(result.success)
        self.assertFalse(result.verified)  # 接口成功不等同于独立回查。
        self.assertEqual("选课成功", result.message)
        self.assertEqual(1, len(self.visited) - before)
        self.assertEqual(len(self.steps), self.index)

    def test_status_does_not_report_open_from_html_alone(self):
        self.steps = self.steps[:7]
        result = self.client.check_open_state()
        self.assertEqual(OpenState.POSSIBLY_OPEN, result.state)
        self.assertEqual(len(self.steps), self.index)

    def test_manual_selected_action_uses_captured_endpoint_after_navigation(self):
        self.steps = self.steps[:7] + [(184, False)]
        with patch("builtins.print"):
            self.assertEqual(0, run_selected(self.settings))
        self.assertEqual(len(self.steps), self.index)

    def test_current_session_error_is_not_hidden_as_success(self):
        self.steps = self.steps[:6]
        workflow = SelectionWorkflow(self.client)
        workflow.open_page()
        response = HttpResult(200, {}, json.dumps({
            "status": "-1", "message": "选课超时或者达到最大在线人数！"
        }, ensure_ascii=False).encode())
        with patch.object(self.client, "request", return_value=response):
            with self.assertRaisesRegex(SelectionSessionError, "electiveStatus.do"):
                workflow.status()
            with self.assertRaisesRegex(SelectionSessionError, "findElectiveFinshCourse.do"):
                workflow.selected_courses()

    def test_malformed_or_mismatched_submission_is_not_success(self):
        workflow = SelectionWorkflow(self.client)
        target = Course.from_dict({"epid": "234838946"})
        for payload in ({"status": 0}, {"status": 0, "result": {"status": 0, "epid": "other"}}):
            with self.subTest(payload=payload), patch.object(
                self.client, "request", return_value=HttpResult(200, {}, json.dumps(payload).encode())
            ):
                with self.assertRaises(SelectionError):
                    workflow.submit_once(target)

    def test_browser_compression_variants(self):
        plain = "选课状态".encode()
        self.assertEqual(plain, JwzxClient._decompress(zlib.compress(plain), {"content-encoding": "deflate"}))
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        self.assertEqual(plain, JwzxClient._decompress(raw.compress(plain) + raw.flush(), {"Content-Encoding": "deflate"}))


if __name__ == "__main__":
    unittest.main()
