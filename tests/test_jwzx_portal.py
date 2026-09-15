from __future__ import annotations

import unittest
from unittest.mock import patch

from course_helper import HttpResult, OpenState
from jwzx_portal import AssetParser, Page, PortalClient, TableParser, extract_assets, extract_tables


class ParserTests(unittest.TestCase):
    def test_extract_assets(self):
        source = """
        <frame src="listLeft.do?randomString=abc">
        <a href="./accessModule.do?moduleId=2050&groupId="><span>学生选课</span></a>
        <form action="/academic/manager/electcourse/elective.do"></form>
        """
        assets = extract_assets(source)
        self.assertIn(("frame", "listLeft.do?randomString=abc", ""), assets)
        self.assertIn(
            ("a", "./accessModule.do?moduleId=2050&groupId=", "学生选课"),
            assets,
        )
        self.assertIn(
            ("form", "/academic/manager/electcourse/elective.do", ""),
            assets,
        )

    def test_extract_tables(self):
        source = """
        <table>
          <tr><th>课程号</th><th>课程名称</th></tr>
          <tr><td>ABC001</td><td>测试课程<br>第一课堂</td></tr>
        </table>
        """
        self.assertEqual(
            [
                [
                    ["课程号", "课程名称"],
                    ["ABC001", "测试课程 第一课堂"],
                ]
            ],
            extract_tables(source),
        )


class FakePortal(PortalClient):
    def __init__(self):
        self._menu = Page(
            "http://example.test/academic/listLeft.do",
            HttpResult(
                200,
                {"Content-Type": "text/html;charset=UTF-8"},
                (
                    '<a href="./accessModule.do?moduleId=2050&groupId=&randomString=x">'
                    "学生选课</a>"
                ).encode(),
            ),
        )
        self.followed = None

    def load_menu(self):
        return self._menu

    def absolute_url(self, value, base=None):
        from urllib.parse import urljoin

        return urljoin(base or "http://example.test/", value)

    def follow(self, path_or_url, **kwargs):
        self.followed = (path_or_url, kwargs)
        return Page(
            path_or_url,
            HttpResult(302, {"Location": "./errorUnOpen.html"}, b""),
        )


class ModuleNavigationTests(unittest.TestCase):
    def test_open_module_uses_dynamic_menu_link(self):
        portal = FakePortal()
        page = portal.open_module(2050)
        self.assertIn("moduleId=2050", page.url)
        self.assertEqual(portal._menu.url, portal.followed[1]["referer"])


class WatchPortal(PortalClient):
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self._menu = None

    def selection_status(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WatchTests(unittest.TestCase):
    @patch("jwzx_portal.time.sleep")
    def test_watch_retries_network_error_and_stops_on_change(self, mocked_sleep):
        portal = WatchPortal(
            [OSError("temporary"), OpenState.CLOSED, OpenState.POSSIBLY_OPEN]
        )
        portal.watch_selection(interval=15, max_errors=2)
        self.assertEqual(3, portal.calls)
        self.assertEqual(2, mocked_sleep.call_count)


if __name__ == "__main__":
    unittest.main()
