#! python3
"""哈尔滨理工大学教务在线请求客户端。

覆盖抓包中已经确认的只读模块：登录、课表、教学计划、校历、成绩、
考试、课程查询、学籍信息和选课入口状态。真正的选课提交接口尚未
出现在抓包中，因此这里不会猜测或发送选课提交请求。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from course_helper import HttpResult, JwzxClient, LoginState, OpenState, Settings


# 凭据优先从配置、环境变量或交互输入读取
USERNAME = os.environ.get("JWZX_USERNAME", "")
PASSWORD = os.environ.get("JWZX_PASSWORD", "")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
EXPORT_DIR = BASE_DIR / "exports"

MODULES = {
    2070: "个人教学计划",
    400: "校历安排",
    202: "课程查询",
    2060: "学籍信息",
    2000: "本学期课表",
    2050: "学生选课",
    2030: "学生考试安排",
    2020: "个人成绩查询",
    2021: "课程成绩",
    2022: "等级考试成绩",
}


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


class AssetParser(HTMLParser):
    """提取页面中的链接、表单、框架和图片地址。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[tuple[str, str, str]] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self._anchor_href = values["href"]
            self._anchor_text = []
        elif tag == "form" and values.get("action"):
            self.urls.append(("form", values["action"] or "", ""))
        elif tag in {"frame", "iframe", "img", "script"} and values.get("src"):
            self.urls.append((tag, values["src"] or "", ""))

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            self.urls.append(("a", self._anchor_href, clean_text("".join(self._anchor_text))))
            self._anchor_href = None
            self._anchor_text = []


class TableParser(HTMLParser):
    """将普通 HTML 表格转换为二维字符串数组。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._table: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in {"th", "td"} and self._row is not None:
            self._cell = []
        elif self._cell is not None and tag in {"br", "p", "div"}:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 1 and tag in {"th", "td"} and self._cell is not None:
            if self._row is not None:
                self._row.append(clean_text("".join(self._cell)))
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            if self._depth == 1 and self._table:
                self.tables.append(self._table)
            self._depth -= 1


def extract_assets(text: str) -> list[tuple[str, str, str]]:
    parser = AssetParser()
    parser.feed(text)
    return parser.urls


def extract_tables(text: str) -> list[list[list[str]]]:
    parser = TableParser()
    parser.feed(text)
    return parser.tables


@dataclass(frozen=True)
class Page:
    url: str
    response: HttpResult


class PortalClient:
    def __init__(self, config_path: Path = DEFAULT_CONFIG) -> None:
        loaded = Settings.load(config_path)
        if loaded.cookie_file == Path("state/session.cookies.txt"):
            loaded = replace(
                loaded,
                cookie_file=Path("state/portal_session.cookies.txt"),
                captcha_file=Path("state/portal_captcha.jpg"),
            )
        self.settings = loaded
        self.http = JwzxClient(self.settings)
        self._menu: Page | None = None

    def absolute_url(self, value: str, base: str | None = None) -> str:
        return urljoin(base or f"{self.settings.base_url}/", value)

    def follow(
        self,
        path_or_url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
        max_redirects: int = 6,
    ) -> Page:
        current = self.absolute_url(path_or_url)
        response = self.http.request(
            current,
            method=method,
            data=data,
            referer=referer,
        )
        for _ in range(max_redirects):
            if response.status not in {301, 302, 303, 307, 308} or not response.location:
                return Page(current, response)
            next_url = self.absolute_url(response.location, current)
            response = self.http.request(next_url, referer=current)
            current = next_url
        raise RuntimeError(f"重定向次数过多：{current}")

    def is_authenticated(self) -> bool:
        if not self.http.has_session():
            return False
        response = self.http.get("/academic/index_new.jsp")
        return response.status == 200 and "frameset_index.jsp" in response.text

    def login_interactive(self, captcha_attempts: int = 5) -> None:
        login_page = self.http.get_login_page()
        if login_page.status != 200 or 'name="j_username"' not in login_page.text:
            raise RuntimeError(
                f"登录页加载失败：HTTP {login_page.status}, Location={login_page.location!r}"
            )

        username = (
            getattr(self.settings, "username", "")
            or os.environ.get("JWZX_USERNAME", "")
            or USERNAME
        )
        password = (
            getattr(self.settings, "password", "")
            or os.environ.get("JWZX_PASSWORD", "")
            or PASSWORD
        )
        if not username:
            username = input("请输入学号：").strip()
        if not password:
            import getpass

            password = getpass.getpass("请输入密码：").strip()

        for attempt in range(1, captcha_attempts + 1):
            image_path = self.http.fetch_captcha()
            print(f"验证码图片：{image_path}")
            if os.name == "nt":
                try:
                    os.startfile(image_path)  # type: ignore[attr-defined]
                except OSError:
                    pass
            code = input("请输入验证码（直接回车取消）：").strip()
            if not code:
                raise RuntimeError("已取消登录")
            if not self.http.validate_captcha(code):
                print(f"验证码错误（{attempt}/{captcha_attempts}），已刷新")
                continue

            result = self.http.login(username, password, code)
            if result.state == LoginState.SUCCESS:
                print("登录成功，Cookie 已自动保存")
                self._menu = None
                return
            if result.state == LoginState.BAD_CREDENTIALS:
                raise RuntimeError("账号或密码错误")
            raise RuntimeError(result.detail)
        raise RuntimeError("验证码连续错误，登录已停止")

    def ensure_authenticated(self) -> None:
        if not self.is_authenticated():
            self.login_interactive()

    def load_menu(self) -> Page:
        self.ensure_authenticated()
        if self._menu is not None:
            return self._menu

        frameset_url = self.absolute_url("/academic/frameset_index.jsp")
        frameset = self.follow(frameset_url)
        if frameset.response.status != 200:
            raise RuntimeError(f"主框架加载失败：HTTP {frameset.response.status}")

        menu_href = next(
            (
                url
                for kind, url, _ in extract_assets(frameset.response.text)
                if kind == "frame" and "listLeft.do" in url
            ),
            None,
        )
        if not menu_href:
            raise RuntimeError("主框架中未找到 listLeft.do 菜单地址")
        menu = self.follow(self.absolute_url(menu_href, frameset.url), referer=frameset.url)
        if menu.response.status != 200:
            raise RuntimeError(f"菜单加载失败：HTTP {menu.response.status}")
        self._menu = menu
        return menu

    def open_module(self, module_id: int) -> Page:
        menu = self.load_menu()
        target = None
        for kind, url, _ in extract_assets(menu.response.text):
            if kind != "a" or "accessModule.do" not in url:
                continue
            values = parse_qs(urlparse(url).query)
            if values.get("moduleId") == [str(module_id)]:
                target = self.absolute_url(url, menu.url)
                break
        if not target:
            label = MODULES.get(module_id, "未知模块")
            raise RuntimeError(f"当前账号菜单中没有 {label}（moduleId={module_id}）")
        return self.follow(target, referer=menu.url)

    def save_page(self, name: str, page: Page, print_rows: int = 20) -> tuple[Path, Path]:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        html_path = EXPORT_DIR / f"{name}_{stamp}.html"
        json_path = EXPORT_DIR / f"{name}_{stamp}.json"
        html_path.write_bytes(page.response.body)

        tables = extract_tables(page.response.text)
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "url": page.url,
            "http_status": page.response.status,
            "tables": tables,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存：{html_path}")
        print(f"已保存：{json_path}")
        self.print_best_table(tables, print_rows)
        return html_path, json_path

    @staticmethod
    def print_best_table(tables: list[list[list[str]]], limit: int) -> None:
        useful = [table for table in tables if table and max(map(len, table), default=0) >= 2]
        if not useful:
            print("页面没有可直接提取的表格，请查看 HTML 文件")
            return
        table = max(useful, key=lambda rows: sum(len(row) for row in rows))
        print("---- 页面主要表格 ----")
        for row in table[:limit]:
            print(" | ".join(cell or "-" for cell in row[:16]))
        if len(table) > limit:
            print(f"... 其余 {len(table) - limit} 行见 JSON 文件")

    def teaching_plan(self) -> None:
        self.save_page("teaching_plan", self.open_module(2070))

    def calendar(self) -> None:
        self.save_page("calendar", self.open_module(400))

    def personal_info(self) -> None:
        page = self.open_module(2060)
        self.save_page("personal_info", page, print_rows=40)
        photo_url = next(
            (
                url
                for kind, url, _ in extract_assets(page.response.text)
                if kind == "img" and "showStudentImage.jsp" in url
            ),
            None,
        )
        if photo_url:
            photo = self.follow(self.absolute_url(photo_url, page.url), referer=page.url)
            if photo.response.status == 200 and photo.response.body:
                EXPORT_DIR.mkdir(parents=True, exist_ok=True)
                photo_path = EXPORT_DIR / "student_photo.jpg"
                photo_path.write_bytes(photo.response.body)
                print(f"照片已保存：{photo_path}")

    def current_courses(self) -> Page:
        page = self.open_module(2000)
        self.save_page("current_courses", page, print_rows=30)
        return page

    def timetable(self) -> None:
        current = self.open_module(2000)
        timetable_url = next(
            (
                url
                for _, url, _ in extract_assets(current.response.text)
                if "showTimetable.do" in url
            ),
            None,
        )
        if not timetable_url:
            match = re.search(r"['\"]([^'\"]*showTimetable\.do\?[^'\"]+)['\"]", current.response.text)
            timetable_url = match.group(1) if match else None
        if not timetable_url:
            raise RuntimeError("本学期课表页面中未找到 showTimetable.do 地址")
        page = self.follow(self.absolute_url(timetable_url, current.url), referer=current.url)
        self.save_page("timetable", page, print_rows=30)

    def exams(self) -> None:
        index = self.open_module(2030)
        if "student/exam/index.jsdo" not in index.url:
            raise RuntimeError(f"考试模块跳转异常：{index.url}")
        pre = self.follow(
            "/academic/manager/examstu/studentQueryAllExamPre.do",
            method="POST",
            data=b"",
            referer=index.url,
        )
        if "studentQueryAllExam.do" not in pre.url:
            raise RuntimeError(f"考试查询跳转异常：{pre.url}")
        self.save_page("exams", pre, print_rows=30)

    def scores(self) -> None:
        for module_id, name in (
            (2020, "personal_scores"),
            (2021, "course_scores"),
            (2022, "level_exam_scores"),
        ):
            page = self.open_module(module_id)
            self.save_page(name, page, print_rows=30)

    def search_courses(self, keyword: str = "", field: str = "coursename") -> None:
        index = self.open_module(202)
        if field not in {"pcourseid", "coursename", "ecoursename"}:
            raise ValueError("课程查询字段必须是 pcourseid、coursename 或 ecoursename")
        labels = {
            "pcourseid": "课程号",
            "coursename": "课程名",
            "ecoursename": "课程英文名",
        }
        form = [
            ("depid", "1"),
            ("trgroup", "-2"),
            ("keyword", field),
            ("keyvalue", keyword),
            ("roomsort", "-2"),
            ("emanner", "-2"),
            ("emode", "-2"),
            ("terms", "2"),
            ("terms", "1"),
            ("ctype", "0"),
            ("status", "-2"),
            ("orderby", "0"),
            ("orderseq", "0"),
            ("depname", "哈尔滨理工大学"),
            ("trgroupname", "全部"),
            ("roomsortname", "全部"),
            ("emannername", "全部"),
            ("emodename", "全部"),
            ("statusname", "全部"),
            ("keywordname", labels[field]),
        ]
        data = urlencode(form, encoding="gbk").encode("ascii")
        page = self.follow(
            "/academic/manager/querycourse/course_list.jsdo",
            method="POST",
            data=data,
            referer=index.url,
        )
        self.save_page("course_search", page, print_rows=50)

    def selection_status(self) -> OpenState:
        self.ensure_authenticated()
        result = self.http.check_open_state()
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{checked_at}] 选课入口：{result.state.value} - {result.detail}")
        if result.state in {OpenState.POSSIBLY_OPEN, OpenState.UNEXPECTED}:
            page = Page(self.absolute_url(self.settings.entry_path), result.response)
            self.save_page("selection_entry", page)
        return result.state

    def watch_selection(self, interval: float = 1, max_errors: int = 10) -> None:
        if interval < 0:
            raise ValueError("监测间隔不能小于 0 秒")
        if max_errors < 1:
            raise ValueError("max-errors 必须大于 0")
        print(f"开始持续监测：每 {interval:g} 秒检查一次，按 Ctrl+C 停止")
        checks = 0
        consecutive_errors = 0
        while True:
            checks += 1
            try:
                state = self.selection_status()
                consecutive_errors = 0
            except (OSError, RuntimeError) as error:
                consecutive_errors += 1
                checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"[{checked_at}] 第 {checks} 次检查失败"
                    f"（连续 {consecutive_errors}/{max_errors} 次）：{error}",
                    file=sys.stderr,
                )
                if consecutive_errors >= max_errors:
                    raise RuntimeError("连续检查失败次数达到上限，监测已停止") from error
                time.sleep(interval)
                continue

            if state == OpenState.AUTH_REQUIRED:
                self._menu = None
                print("登录态已失效，下次检查前将重新登录")
                time.sleep(1)
                continue
            if state != OpenState.CLOSED:
                print("\a选课入口状态已变化，持续监测结束")
                return
            print(f"第 {checks} 次检查完成；系统仍未开放")
            time.sleep(interval)

    def arbitrary_module(self, module_id: int) -> None:
        page = self.open_module(module_id)
        name = MODULES.get(module_id, f"module_{module_id}")
        safe_name = re.sub(r"[^0-9A-Za-z_-]+", "_", name).strip("_") or f"module_{module_id}"
        self.save_page(safe_name, page)

    def logout(self) -> None:
        if self.http.logout():
            print("已退出登录并删除本地 Cookie")
            self._menu = None
            return
        raise RuntimeError("退出登录失败，本地 Cookie 未删除")


def run_all(portal: PortalClient) -> None:
    jobs: list[tuple[str, Callable[[], None]]] = [
        ("课表", portal.timetable),
        ("个人教学计划", portal.teaching_plan),
        ("校历", portal.calendar),
        ("成绩", portal.scores),
        ("考试", portal.exams),
        ("课程目录", portal.search_courses),
        ("学籍信息", portal.personal_info),
        ("选课入口", portal.selection_status),
    ]
    for label, job in jobs:
        print(f"\n===== {label} =====")
        job()


def interactive_menu(portal: PortalClient) -> int:
    actions: dict[str, tuple[str, Callable[[], object]]] = {
        "1": ("本学期课表", portal.timetable),
        "2": ("个人教学计划", portal.teaching_plan),
        "3": ("校历安排", portal.calendar),
        "4": ("全部成绩", portal.scores),
        "5": ("考试安排", portal.exams),
        "6": ("课程查询", lambda: portal.search_courses(input("课程名关键字（留空查询全部）：").strip())),
        "7": ("学籍信息", portal.personal_info),
        "8": ("选课开放状态", portal.selection_status),
        "9": ("持续监测选课开放", portal.watch_selection),
        "10": ("导出全部只读模块", lambda: run_all(portal)),
        "0": ("退出登录", portal.logout),
    }
    while True:
        print("\n教务在线")
        for key, (label, _) in actions.items():
            print(f"{key}. {label}")
        print("q. 退出程序")
        choice = input("请选择：").strip().lower()
        if choice == "q":
            return 0
        item = actions.get(choice)
        if not item:
            print("无效选项")
            continue
        try:
            item[1]()
        except (OSError, RuntimeError, ValueError) as error:
            print(f"错误：{error}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="教务在线请求客户端")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--username", default="", help="学号（未提供则从配置、环境变量或交互输入读取）")
    parser.add_argument("--password", default="", help="密码（未提供则从配置、环境变量或交互输入读取）")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="启动交互菜单（默认）")
    sub.add_parser("login", help="登录并保存会话")
    sub.add_parser("timetable", help="获取本学期课表")
    sub.add_parser("plan", help="获取个人教学计划")
    sub.add_parser("calendar", help="获取校历")
    sub.add_parser("scores", help="获取个人、课程和等级考试成绩")
    sub.add_parser("exams", help="获取考试安排")
    search = sub.add_parser("courses", help="查询课程目录")
    search.add_argument("--keyword", default="")
    search.add_argument(
        "--field",
        choices=("pcourseid", "coursename", "ecoursename"),
        default="coursename",
    )
    sub.add_parser("info", help="获取学籍信息")
    sub.add_parser("selection", help="检查选课入口")
    watch = sub.add_parser("watch", help="持续监测选课入口")
    watch.add_argument("--interval", type=float, default=30)
    watch.add_argument("--max-errors", type=int, default=5)
    module = sub.add_parser("module", help="按 moduleId 获取其他菜单模块")
    module.add_argument("module_id", type=int)
    sub.add_parser("all", help="导出全部已实现只读模块")
    sub.add_parser("logout", help="退出登录并删除本地 Cookie")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    portal = PortalClient(args.config)
    if getattr(args, "username", ""):
        portal.settings = replace(portal.settings, username=args.username)
    if getattr(args, "password", ""):
        portal.settings = replace(portal.settings, password=args.password)
    command = args.command or "menu"
    try:
        if command == "menu":
            return interactive_menu(portal)
        if command == "login":
            portal.ensure_authenticated()
            print("当前已登录")
        elif command == "timetable":
            portal.timetable()
        elif command == "plan":
            portal.teaching_plan()
        elif command == "calendar":
            portal.calendar()
        elif command == "scores":
            portal.scores()
        elif command == "exams":
            portal.exams()
        elif command == "courses":
            portal.search_courses(args.keyword, args.field)
        elif command == "info":
            portal.personal_info()
        elif command == "selection":
            portal.selection_status()
        elif command == "watch":
            portal.watch_selection(args.interval, args.max_errors)
        elif command == "module":
            portal.arbitrary_module(args.module_id)
        elif command == "all":
            run_all(portal)
        elif command == "logout":
            portal.logout()
        else:
            raise RuntimeError(f"未知命令：{command}")
        return 0
    except KeyboardInterrupt:
        print("\n已停止")
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
