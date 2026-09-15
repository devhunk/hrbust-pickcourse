from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import course_helper
from course_helper import JwzxClient, LoginState, Settings
from selection_workflow import SelectionWorkflow, parse_json_payload


class MockJwzxHandler(BaseHTTPRequestHandler):
    selected_epids: set[str] = set()
    full_selected_requests = 0
    request_paths: list[str] = []

    def log_message(self, format, *args):  # noqa: A002, ANN001
        return

    def _send(self, status, body=b"", headers=None):  # noqa: ANN001
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        type(self).request_paths.append(path)
        cookie = self.headers.get("Cookie", "")
        if path == "/academic/common/security/login.jsp":
            body = '<input name="j_username">用户登录'.encode("gbk")
            self._send(
                200,
                body,
                {
                    "Content-Type": "text/html;charset=GBK",
                    "Set-Cookie": "JSESSIONID=anon; Path=/academic; HttpOnly",
                },
            )
            return
        if path == "/academic/getCaptcha.do":
            if "JSESSIONID=anon" not in cookie:
                self._send(403)
                return
            self._send(200, b"fake-jpeg", {"Content-Type": "image/jpeg;charset=UTF-8"})
            return
        if path == "/academic/index_new.jsp":
            if "JSESSIONID=auth" not in cookie:
                self._send(302, headers={"Location": "/academic/common/security/login.jsp"})
                return
            self._send(
                200,
                b'<iframe src="frameset_index.jsp"></iframe>',
                {"Content-Type": "text/html;charset=GBK"},
            )
            return
        if path == "/academic/frameset_index.jsp":
            self._send(200, b'<frame src="listLeft.do?randomString=menu-token">')
            return
        if path == "/academic/listLeft.do":
            self._send(200, b'<a href="accessModule.do?moduleId=2050&amp;groupId=&amp;randomString=entry-token">select</a>')
            return
        if path == "/academic/accessModule.do":
            self._send(302, headers={"Location": "/academic/student/selectcoursedb/jumppage.jsp?randomString=entry-token&groupId=&moduleId=2050"})
            return
        if path == "/academic/student/selectcoursedb/jumppage.jsp":
            self._send(200, b'<form action="/academic/manager/electcourse/elective.do" method="get"></form>')
            return
        if path == "/academic/manager/electcourse/electiveMgs.do":
            self._send(200, b'{"phase":"","finshcourse":{}}')
            return
        if path == "/academic/logout_security_check":
            if "JSESSIONID=auth" not in cookie:
                self._send(302, headers={"Location": "/academic/common/security/login.jsp"})
                return
            self._send(
                302,
                headers={
                    "Location": "/academic/index.jsp",
                    "Set-Cookie": "JSESSIONID=anon2; Path=/academic; HttpOnly",
                },
            )
            return
        if path == "/academic/manager/electcourse/elective.do":
            if "JSESSIONID=auth" not in cookie:
                self._send(302, headers={"Location": "/academic/common/security/login.jsp"})
                return
            body = (
                '<script>var student = {"userid":"740151","username":"student"};</script>'
                '<script>$.get("electiveStatus.do")</script>'
            ).encode()
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        if path == "/academic/manager/electcourse/electiveStatus.do":
            body = b'{"status":0,"datas":{"phase":"1"}}'
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        if path == "/academic/tmpfile/s740151.json":
            body = (
                'jcallback([{"epid":"234787716","cid":"275999",'
                '"pcourseid":"090526TW01W1","coursename":"大学英语-I",'
                '"cseq":"64","alias":"计算机26-A4","teachernames":"王老师",'
                '"classes":"集成26-1","prop":"0","credit":"2"}])'
            ).encode()
            self._send(200, body, {"Content-Type": "application/json"})
            return
        if path == "/academic/manager/electcourse/getCaptcha.do":
            self._send(200, b"selection-jpeg", {"Content-Type": "image/jpeg;charset=UTF-8"})
            return
        if path == "/academic/manager/electcourse/findElectiveFinshCourse.do":
            type(self).full_selected_requests += 1
            rows = [
                {
                    "epid": epid,
                    "pcourseid": "090526TW01W1",
                    "coursename": "大学英语-I",
                    "cseq": "64",
                    "alias": "计算机26-A4",
                }
                for epid in sorted(self.selected_epids)
            ]
            body = ("{\"status\":\"0\",\"datas\":{\"scheduleScoreList\":" + str(rows).replace("'", '"') + "}}").encode()
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        if path == "/academic/manager/electcourse/electiveSelectCourseAdd.do":
            epid = parse_qs(parsed.query).get("epid", [""])[0]
            self.selected_epids.add(epid)
            body = (
                '{"status":"0","result":{"type":"1","status":"0",'
                f'"epid":"{epid}","message":"选课成功"}}'
                "}"
            ).encode()
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        self._send(404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        type(self).request_paths.append(parsed.path)
        cookie = self.headers.get("Cookie", "")
        if parsed.path == "/academic/manager/electcourse/findCoursecapabilityAmount.do":
            if "JSESSIONID=auth" not in cookie:
                self._send(403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("ascii"))
            epids = form.get("epid", [])
            body = (
                "["
                + ",".join(
                    f'{{"amount":4,"remainCapability":43,"courseCapability":47,"epid":{epid}}}'
                    for epid in epids
                )
                + "]"
            ).encode()
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        if parsed.path == "/academic/manager/electcourse/checkCaptcha.do":
            if "JSESSIONID=auth" not in cookie:
                self._send(403)
                return
            code = parse_qs(parsed.query).get("captchaCode", [""])[0]
            self._send(200, b"true" if code == "6904" else b"false")
            return
        if "JSESSIONID=anon" not in cookie:
            self._send(403)
            return
        if parsed.path == "/academic/checkCaptcha.do":
            code = parse_qs(parsed.query).get("captchaCode", [""])[0]
            body = b"true" if code == "1234" else b"false"
            self._send(200, body, {"Content-Type": "text/html;charset=UTF-8"})
            return
        if parsed.path == "/academic/j_acegi_security_check":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("ascii"))
            if form.get("j_password") == ["correct-password"]:
                self._send(
                    302,
                    headers={
                        "Location": "/academic/index_new.jsp",
                        "Set-Cookie": "JSESSIONID=auth; Path=/academic; HttpOnly",
                    },
                )
            else:
                self._send(
                    302,
                    headers={"Location": "/academic/common/security/login.jsp?login_error=1"},
                )
            return
        self._send(404)


class JwzxClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockJwzxHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockJwzxHandler.selected_epids = set()
        MockJwzxHandler.full_selected_requests = 0
        MockJwzxHandler.request_paths = []
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.settings = Settings(
            base_url=base_url,
            entry_path="/academic/manager/electcourse/elective.do",
            login_page_path="/academic/common/security/login.jsp",
            captcha_path="/academic/getCaptcha.do",
            captcha_check_path="/academic/checkCaptcha.do",
            login_submit_path="/academic/j_acegi_security_check",
            logout_path="/academic/logout_security_check",
            referer=f"{base_url}/academic/common/security/login.jsp",
            session_env="JWZX_TEST_UNUSED_SESSION",
            timeout_seconds=2,
            poll_interval_seconds=30,
            snapshot_dir=root / "snapshots",
            cookie_file=root / "session.cookies.txt",
            captcha_file=root / "captcha.jpg",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_login_rotates_and_persists_cookie_then_logout_clears_it(self):
        client = JwzxClient(self.settings)
        page = client.get_login_page()
        self.assertIn("用户登录", page.text)
        self.assertTrue(client.has_session())

        captcha = client.fetch_captcha()
        self.assertEqual(b"fake-jpeg", captcha.read_bytes())
        self.assertFalse(client.validate_captcha("0000"))
        self.assertTrue(client.validate_captcha("1234"))

        result = client.login("student", "correct-password", "1234")
        self.assertEqual(LoginState.SUCCESS, result.state)
        self.assertIn("auth", [cookie.value for cookie in client.cookie_jar])

        restored = JwzxClient(self.settings)
        self.assertIn("auth", [cookie.value for cookie in restored.cookie_jar])
        self.assertTrue(restored.logout())
        self.assertFalse(self.settings.cookie_file.exists())

    def test_bad_password_is_distinguished(self):
        client = JwzxClient(self.settings)
        client.get_login_page()
        self.assertTrue(client.validate_captcha("1234"))
        result = client.login("student", "wrong-password", "1234")
        self.assertEqual(LoginState.BAD_CREDENTIALS, result.state)
        self.assertIn("anon", [cookie.value for cookie in client.cookie_jar])

    def test_full_selection_flow_uses_only_add_request_after_captcha(self):
        client = JwzxClient(self.settings)
        client.get_login_page()
        result = client.login("student", "correct-password", "1234")
        self.assertEqual(LoginState.SUCCESS, result.state)

        workflow = SelectionWorkflow(client)
        self.assertEqual("740151", workflow.open_page())
        self.assertEqual(0, int(workflow.status()["status"]))
        courses = workflow.list_courses()
        self.assertEqual(1, len(courses))
        course = courses[0]
        self.assertEqual("234787716", course.epid)
        self.assertEqual([course], workflow.filter_courses(courses, alias="A4"))

        capacity = workflow.capacities([course.epid])[course.epid]
        self.assertEqual(43, capacity.remaining)
        self.assertEqual(b"selection-jpeg", workflow.fetch_captcha().read_bytes())
        self.assertFalse(workflow.validate_captcha("0000"))
        self.assertTrue(workflow.validate_captcha("6904"))

        MockJwzxHandler.request_paths = []
        outcome = workflow.submit_once(course)
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.verified)
        self.assertEqual(
            ["/academic/manager/electcourse/electiveSelectCourseAdd.do"],
            MockJwzxHandler.request_paths,
        )
        self.assertEqual(0, MockJwzxHandler.full_selected_requests)
        self.assertTrue(workflow.is_selected(course.epid))
        self.assertEqual(1, MockJwzxHandler.full_selected_requests)

    def test_jsonp_parser(self):
        self.assertEqual([{"epid": "1"}], parse_json_payload('jcallback([{"epid":"1"}]);'))

    @patch("course_helper.run_courses", return_value=0)
    @patch("builtins.input", side_effect=["2", "q"])
    def test_menu_returns_after_action_and_only_quits_on_q(self, _input, run_courses):
        self.assertEqual(0, course_helper.run_menu(self.settings))
        run_courses.assert_called_once()


if __name__ == "__main__":
    unittest.main()
