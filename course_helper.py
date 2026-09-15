#! python3
"""哈尔滨理工大学教务系统登录、查课、容量监测和选课辅助脚本。"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import logging
import os
import random
import re
import sys
import time
import zlib
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener, getproxies

from selection_workflow import (
    AuthRequiredError,
    Course,
    SelectionClosedError,
    SelectionError,
    SelectionOutcome,
    SelectionWorkflow,
)

DEFAULT_CONFIG = Path(__file__).with_name("config.json")

# 选课凭据优先从配置、环境变量或交互输入读取
SELECT_USERNAME = os.environ.get("JWZX_USERNAME", "")
SELECT_PASSWORD = os.environ.get("JWZX_PASSWORD", "")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s.%(msecs)03d] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("JwzxProbe")


def recognize_captcha_digits(image_path: Path) -> str | None:
    """使用项目现有 ddddocr 识别四位数字验证码；不可用时返回 None。"""
    try:
        import ddddocr  # type: ignore[import-not-found]
    except ImportError:
        return None

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            recognizer = ddddocr.DdddOcr(show_ad=False)
        except TypeError:
            recognizer = ddddocr.DdddOcr()
        recognizer.set_ranges("0123456789")
        value = recognizer.classification(image_path.read_bytes())
    digits = re.sub(r"\D", "", str(value))
    return digits if len(digits) == 4 else None


def obtain_valid_captcha(
    fetch_image,
    validate_code,
    *,
    ocr_attempts: int,
    label: str,
) -> str | None:
    """自动识别并在线预校验，失败后保存新图供人工输入。"""
    for attempt in range(1, max(0, ocr_attempts) + 1):
        image_path = fetch_image()
        code = recognize_captcha_digits(image_path)
        if code is None:
            logger.info("%s OCR 不可用或结果格式不正确，转为手动输入", label)
            break
        if validate_code(code):
            logger.info("%s OCR 预校验通过（第 %d 次）", label, attempt)
            return code
        logger.warning("%s OCR 预校验失败（第 %d/%d 次）", label, attempt, ocr_attempts)

    while True:
        image_path = fetch_image()
        print(f"{label}图片：{image_path}")
        code = input(f"请输入{label}（直接回车取消）：").strip()
        if not code:
            return None
        if validate_code(code):
            return code
        print(f"{label}错误，已刷新")


class OpenState(str, Enum):
    CLOSED = "closed"
    AUTH_REQUIRED = "auth_required"
    POSSIBLY_OPEN = "possibly_open"
    UNEXPECTED = "unexpected"


class LoginState(str, Enum):
    SUCCESS = "success"
    BAD_CREDENTIALS = "bad_credentials"
    UNEXPECTED = "unexpected"


class DetectionRules:
    UNOPEN_MARKERS: tuple[str, ...] = ("errorunopen.html",)
    LOGOUT_MARKERS: tuple[str, ...] = ("login", "index.jsp", "logout")
    LOGIN_BODY_MARKERS: tuple[str, ...] = (
        'name="j_password"',
        "用户登录",
        "统一身份认证",
    )


@dataclass(frozen=True)
class Settings:
    base_url: str
    entry_path: str
    login_page_path: str
    captcha_path: str
    captcha_check_path: str
    login_submit_path: str
    logout_path: str
    referer: str
    session_env: str
    timeout_seconds: float
    poll_interval_seconds: float
    snapshot_dir: Path
    cookie_file: Path
    captcha_file: Path
    trace_http: bool = False
    username: str = ""
    password: str = ""

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                raw = json.load(file)

        base_url = str(raw.get("base_url", "http://jwzx.hrbust.edu.cn")).rstrip("/")

        return cls(
            base_url=base_url,
            entry_path=str(raw.get("entry_path", "/academic/manager/electcourse/elective.do")),
            login_page_path=str(
                raw.get("login_page_path", "/academic/common/security/login.jsp")
            ),
            captcha_path=str(raw.get("captcha_path", "/academic/getCaptcha.do")),
            captcha_check_path=str(
                raw.get("captcha_check_path", "/academic/checkCaptcha.do")
            ),
            login_submit_path=str(
                raw.get("login_submit_path", "/academic/j_acegi_security_check")
            ),
            logout_path=str(raw.get("logout_path", "/academic/logout_security_check")),
            referer=str(
                raw.get(
                    "referer",
                    f"{base_url}/academic/student/selectcoursedb/jumppage.jsp?groupId=&moduleId=2050",
                )
            ),
            session_env=str(raw.get("session_env", "JWZX_JSESSIONID")),
            timeout_seconds=float(raw.get("timeout_seconds", 5.0)),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 30.0)),
            snapshot_dir=Path(raw.get("snapshot_dir", "snapshots")),
            cookie_file=Path(raw.get("cookie_file", "state/session.cookies.txt")),
            captcha_file=Path(raw.get("captcha_file", "state/captcha.jpg")),
            trace_http=bool(raw.get("trace_http", False)),
            username=str(raw.get("username", os.environ.get("JWZX_USERNAME", ""))),
            password=str(raw.get("password", os.environ.get("JWZX_PASSWORD", ""))),
        )


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return ""

    @property
    def location(self) -> str:
        return self.header("Location")

    @property
    def text(self) -> str:
        content_type = self.header("Content-Type")
        match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
        encodings = [match.group(1)] if match else []
        encodings.extend(["utf-8", "gb18030"])
        for encoding in dict.fromkeys(encodings):
            try:
                return self.body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class StatusResult:
    state: OpenState
    detail: str
    response: HttpResult


@dataclass(frozen=True)
class LoginResult:
    state: LoginState
    detail: str
    response: HttpResult


class NoRedirectHandler(HTTPRedirectHandler):
    """拦截 3xx 重定向以抓取真实的 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class JwzxClient:
    """HTTP 客户端，自动接收、保存并恢复 JSESSIONID。"""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cookie_path = self._workspace_path(settings.cookie_file)
        self.cookie_jar = MozillaCookieJar(str(self.cookie_path))
        self._load_cookies()
        self._import_legacy_session()
        self.opener = build_opener(
            HTTPCookieProcessor(self.cookie_jar),
            NoRedirectHandler(),
        )
        self._request_count = 0
        if settings.trace_http:
            proxy = getproxies().get(urlparse(settings.base_url).scheme, "")
            proxy_parts = urlparse(proxy if "://" in proxy else "http://" + proxy)
            logger.info(
                "HTTP 诊断：Python=%s，脚本=%s，系统代理=%s",
                sys.executable, Path(__file__).resolve(),
                f"{proxy_parts.hostname}:{proxy_parts.port}" if proxy else "无",
            )

    @staticmethod
    def _workspace_path(path: Path) -> Path:
        return path if path.is_absolute() else Path(__file__).parent / path

    def _load_cookies(self) -> None:
        if not self.cookie_path.exists():
            return
        try:
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Cookie 文件无法读取：{self.cookie_path}: {error}") from error

    def _import_legacy_session(self) -> None:
        if any(cookie.name == "JSESSIONID" for cookie in self.cookie_jar):
            return
        session_id = os.environ.get(self.settings.session_env, "").strip()
        if not session_id:
            return
        hostname = urlparse(self.settings.base_url).hostname
        if not hostname:
            raise RuntimeError("base_url 缺少有效主机名")
        self.cookie_jar.set_cookie(
            Cookie(
                version=0,
                name="JSESSIONID",
                value=session_id,
                port=None,
                port_specified=False,
                domain=hostname,
                domain_specified=True,
                domain_initial_dot=False,
                path="/academic",
                path_specified=True,
                secure=False,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
        )
        self.save_cookies()

    def has_session(self) -> bool:
        return any(cookie.name == "JSESSIONID" for cookie in self.cookie_jar)

    def require_session(self) -> None:
        if not self.has_session():
            raise RuntimeError("没有可用会话，请先运行 login")

    def save_cookies(self) -> None:
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_jar.save(ignore_discard=True, ignore_expires=True)

    def clear_local_session(self) -> None:
        self.cookie_jar.clear()
        if self.cookie_path.exists():
            self.cookie_path.unlink()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
        custom_timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResult:
        timeout = custom_timeout or self.settings.timeout_seconds
        target_url = urljoin(f"{self.settings.base_url}/", path.lstrip("/"))

        request_headers = {
            "User-Agent": self.DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Referer": referer or self.settings.referer,
        }
        if headers:
            request_headers.update(headers)

        request = Request(
            target_url,
            headers=request_headers,
            data=data,
            method=method,
        )
        self._request_count += 1
        trace_id = self._request_count
        if self.settings.trace_http:
            # 只记录参数名；密码、验证码和 Cookie 值不进入日志。
            query_keys = [key for key, _ in parse_qsl(urlparse(target_url).query)]
            logger.info(
                "HTTP -> #%d %s %s 参数=%s Body=%d字节 会话=%s",
                trace_id, method, urlparse(target_url).path, query_keys,
                len(data or b""), "有" if self.has_session() else "无",
            )
            logger.info(
                "HTTP 请求头：Accept=%s | Origin=%s | Referer路径=%s",
                request_headers["Accept"], request_headers.get("Origin", "无"),
                urlparse(request_headers["Referer"]).path,
            )

        try:
            with self.opener.open(request, timeout=timeout) as resp:
                body = resp.read()
                headers = dict(resp.headers.items())
                result = HttpResult(resp.status, headers, self._decompress(body, headers))
        except HTTPError as error:
            with error:
                body = error.read()
                headers = dict(error.headers.items())
                result = HttpResult(error.code, headers, self._decompress(body, headers))
        except Exception as error:
            if self.settings.trace_http:
                logger.error("HTTP !! #%d 未取得响应：%s", trace_id, type(error).__name__)
            raise
        if self.settings.trace_http:
            logger.info(
                "HTTP <- #%d 状态=%d Content-Type=%s Body=%d字节",
                trace_id, result.status, result.header("Content-Type"), len(result.body),
            )
        self.save_cookies()
        return result

    def get(
        self,
        path: str,
        custom_timeout: float | None = None,
        referer: str | None = None,
    ) -> HttpResult:
        return self.request(path, referer=referer, custom_timeout=custom_timeout)

    def get_login_page(self) -> HttpResult:
        return self.get(self.settings.login_page_path)

    def fetch_captcha(self) -> Path:
        separator = "&" if "?" in self.settings.captcha_path else "?"
        captcha_url = f"{self.settings.captcha_path}{separator}{random.random()}"
        response = self.get(
            captcha_url,
            referer=urljoin(self.settings.base_url, self.settings.login_page_path),
        )
        if response.status != 200 or not response.header("Content-Type").lower().startswith(
            "image/jpeg"
        ):
            raise RuntimeError(
                f"获取验证码失败：HTTP {response.status}, {response.header('Content-Type')}"
            )
        target = self._workspace_path(self.settings.captcha_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.body)
        return target

    def validate_captcha(self, captcha_code: str) -> bool:
        query = urlencode({"captchaCode": captcha_code})
        response = self.request(
            f"{self.settings.captcha_check_path}?{query}",
            method="POST",
            referer=urljoin(self.settings.base_url, self.settings.login_page_path),
            headers={
                "Accept": "text/plain, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.settings.base_url,
            },
        )
        value = response.text.strip().lower()
        if response.status != 200 or value not in {"true", "false"}:
            raise RuntimeError(f"验证码校验返回异常：HTTP {response.status}, {value[:80]!r}")
        return value == "true"

    def login(self, username: str, password: str, captcha_code: str) -> LoginResult:
        payload = urlencode(
            {
                "j_username": username,
                "j_password": password,
                "j_captcha": captcha_code,
            }
        ).encode("ascii")
        response = self.request(
            self.settings.login_submit_path,
            method="POST",
            data=payload,
            referer=urljoin(self.settings.base_url, self.settings.login_page_path),
            headers={
                **SelectionWorkflow.DOCUMENT_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.settings.base_url,
            },
        )
        location = response.location.lower()
        if response.status in {301, 302, 303, 307, 308}:
            if "index_new.jsp" in location:
                verify = self.get("/academic/index_new.jsp")
                if verify.status == 200 and "frameset_index.jsp" in verify.text:
                    return LoginResult(LoginState.SUCCESS, "登录成功，会话已更新", response)
                return LoginResult(LoginState.UNEXPECTED, "登录跳转成功，但主页校验失败", verify)
            if "login.jsp?login_error=1" in location:
                return LoginResult(
                    LoginState.BAD_CREDENTIALS,
                    "用户名或密码错误；下次尝试需要重新获取验证码",
                    response,
                )
        return LoginResult(
            LoginState.UNEXPECTED,
            f"未识别的登录响应：HTTP {response.status}, Location={response.location!r}",
            response,
        )

    def logout(self) -> bool:
        self.require_session()
        response = self.get(self.settings.logout_path)
        success = response.status in {301, 302, 303, 307, 308} and "index.jsp" in (
            response.location.lower()
        )
        if success:
            self.clear_local_session()
        return success

    @staticmethod
    def _decompress(body: bytes, headers: dict[str, str]) -> bytes:
        encoding = next(
            (value for key, value in headers.items() if key.lower() == "content-encoding"), ""
        ).lower()
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        return body

    def check_open_state(self, custom_timeout: float | None = None) -> StatusResult:
        client = self if custom_timeout is None else JwzxClient(
            replace(self.settings, timeout_seconds=custom_timeout)
        )
        workflow = SelectionWorkflow(client)
        try:
            workflow.open_page()
            workflow.status()
            state, detail = OpenState.POSSIBLY_OPEN, "选课状态接口确认可进入"
        except AuthRequiredError as error:
            state, detail = OpenState.AUTH_REQUIRED, str(error)
        except SelectionClosedError as error:
            state, detail = OpenState.CLOSED, str(error)
        except SelectionError as error:
            state, detail = OpenState.UNEXPECTED, str(error)
        if workflow.last_response is None:
            raise RuntimeError(detail)
        return StatusResult(state, detail, workflow.last_response)


class ProbeService:
    @staticmethod
    def save_snapshot(settings: Settings, result: StatusResult) -> Path:
        target_dir = settings.snapshot_dir
        if not target_dir.is_absolute():
            target_dir = Path(__file__).parent / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        file_path = target_dir / f"entry_{timestamp}_{result.response.status}.html"
        file_path.write_bytes(result.response.body)
        return file_path

    @classmethod
    def execute_watch_loop(cls, settings: Settings, interval: float, max_checks: int) -> int:
        if interval < 5:
            raise ValueError("监测间隔不得小于 5 秒")
        client = JwzxClient(settings)
        client.require_session()
        count = 0
        relogins = 0
        while max_checks == 0 or count < max_checks:
            count += 1
            try:
                result = client.check_open_state()
                if result.state == OpenState.CLOSED:
                    logger.info("第 %d 次：未开放", count)
                elif result.state == OpenState.AUTH_REQUIRED:
                    relogins += 1
                    if relogins > 3:
                        raise RuntimeError("会话连续失效，自动重新登录已停止")
                    logger.warning("会话已失效，正在自动重新登录并更换 Cookie")
                    if run_login(settings, 3) != 0:
                        return 2
                    client = JwzxClient(settings)
                    continue
                else:
                    snapshot = cls.save_snapshot(settings, result)
                    logger.warning("状态变化：[%s] %s", result.state.value, result.detail)
                    logger.info("响应快照：%s", snapshot)
                    return 2
            except (URLError, TimeoutError) as error:
                logger.warning("第 %d 次网络异常：%s", count, error)
            if max_checks == 0 or count < max_checks:
                time.sleep(interval)
        logger.info("监测完成（%d 次），未见开放", count)
        return 0


def _target_filters(args: argparse.Namespace) -> dict[str, str]:
    return {
        "epid": getattr(args, "epid", "") or "",
        "course_code": getattr(args, "course_code", "") or "",
        "name": getattr(args, "name", "") or "",
        "teacher": getattr(args, "teacher", "") or "",
        "alias": getattr(args, "alias", "") or "",
    }


def _print_courses(courses: list[Course], capacities: dict[str, Any] | None = None) -> None:
    if not courses:
        print("没有匹配课程")
        return
    for index, course in enumerate(courses, 1):
        suffix = ""
        capacity = (capacities or {}).get(course.epid)
        if capacity:
            suffix = f" | 余量={capacity.remaining}/{capacity.total}"
        print(f"{index:>3}. {course.display()}{suffix}")


def _resolve_unique_course(workflow: SelectionWorkflow, args: argparse.Namespace) -> Course:
    filters = _target_filters(args)
    if not any(filters.values()):
        raise ValueError("必须至少提供 --epid、--course-code、--name、--teacher 或 --alias")
    matches = workflow.filter_courses(workflow.list_courses(), **filters)
    if not matches:
        raise RuntimeError("没有找到符合条件的待选课程")
    if len(matches) > 1:
        _print_courses(matches)
        raise RuntimeError("目标不唯一，请增加筛选条件或直接使用 --epid")
    return matches[0]


def _confirm_course(course: Course, assume_yes: bool) -> bool:
    print(f"目标课程：{course.display()}")
    if assume_yes:
        return True
    return input("确认选择该教学班？输入 yes 继续：").strip().lower() == "yes"


def run_courses(settings: Settings, args: argparse.Namespace) -> int:
    workflow = SelectionWorkflow(JwzxClient(settings))
    workflow.open_page()
    workflow.status()
    courses = workflow.filter_courses(workflow.list_courses(), **_target_filters(args))
    capacities: dict[str, Any] = {}
    for start in range(0, len(courses), 100):
        capacities.update(workflow.capacities([c.epid for c in courses[start : start + 100]]))
    _print_courses(courses, capacities)
    return 0 if courses else 2


def run_selected(settings: Settings) -> int:
    workflow = SelectionWorkflow(JwzxClient(settings))
    workflow.open_page()
    workflow.status()
    rows = workflow.selected_courses()
    if not rows:
        print("当前没有已选课程")
        return 0
    for index, row in enumerate(rows, 1):
        print(
            f"{index:>3}. epid={row.get('epid', '')} | {row.get('coursename', '')} | "
            f"课号={row.get('pcourseid', '')} | 课序={row.get('cseq', '')} | "
            f"班别名={row.get('alias', '')}"
        )
    return 0


def _obtain_selection_captcha(workflow: SelectionWorkflow, ocr_attempts: int) -> str | None:
    return obtain_valid_captcha(
        workflow.fetch_captcha,
        workflow.validate_captcha,
        ocr_attempts=ocr_attempts,
        label="选课验证码",
    )


def _report_outcome(outcome: SelectionOutcome) -> int:
    if outcome.success:
        logger.info("选课接口返回成功：%s", outcome.message)
        return 0
    logger.error("选课失败：%s", outcome.message)
    return 4


def run_select(settings: Settings, args: argparse.Namespace) -> int:
    workflow = SelectionWorkflow(JwzxClient(settings))
    workflow.open_page()
    workflow.status()
    course = _resolve_unique_course(workflow, args)
    if not _confirm_course(course, args.yes):
        print("已取消")
        return 1
    capacity = workflow.capacities([course.epid]).get(course.epid)
    if capacity is None:
        raise RuntimeError("容量接口没有返回目标教学班")
    logger.info("当前余量：%d/%d", capacity.remaining, capacity.total)
    if capacity.remaining <= 0:
        logger.error("当前无余量；可改用 grab 持续等待")
        return 4
    if _obtain_selection_captcha(workflow, args.ocr_attempts) is None:
        print("已取消")
        return 1
    return _report_outcome(workflow.submit_once(course))


def run_grab(settings: Settings, args: argparse.Namespace) -> int:
    if args.interval < 5:
        raise ValueError("抢课监测间隔不得小于 5 秒")
    if args.max_checks < 0 or args.max_submit_attempts < 1:
        raise ValueError("次数参数无效")

    workflow = SelectionWorkflow(JwzxClient(settings))
    course: Course | None = None
    checks = 0
    submit_attempts = 0
    consecutive_errors = 0
    relogins = 0

    while args.max_checks == 0 or checks < args.max_checks:
        checks += 1
        try:
            if course is None:
                workflow.open_page()
                workflow.status()
                course = _resolve_unique_course(workflow, args)
                if not _confirm_course(course, args.yes):
                    print("已取消")
                    return 1
                logger.info("开始监测目标教学班，每 %.1f 秒检查一次", args.interval)

            capacity = workflow.capacities([course.epid]).get(course.epid)
            if capacity is None:
                raise RuntimeError("容量接口没有返回目标教学班")
            consecutive_errors = 0
            logger.info(
                "第 %d 次：%s，余量 %d/%d",
                checks,
                course.name,
                capacity.remaining,
                capacity.total,
            )
            if capacity.remaining > 0:
                if _obtain_selection_captcha(workflow, args.ocr_attempts) is None:
                    print("已取消")
                    return 1
                submit_attempts += 1
                outcome = workflow.submit_once(course)
                if outcome.success or outcome.status == 0:
                    return _report_outcome(outcome)
                retryable = any(word in outcome.message for word in ("已满", "容量", "稍后", "繁忙"))
                logger.warning(
                    "第 %d/%d 次提交未成功：%s",
                    submit_attempts,
                    args.max_submit_attempts,
                    outcome.message,
                )
                if not retryable or submit_attempts >= args.max_submit_attempts:
                    return 4
        except SelectionClosedError as error:
            consecutive_errors = 0
            logger.info("第 %d 次：%s", checks, error)
        except AuthRequiredError:
            relogins += 1
            if relogins > 3:
                raise RuntimeError("会话连续失效，自动重新登录已停止")
            logger.warning("会话已失效，正在自动重新登录并更换 Cookie")
            if run_login(settings, args.ocr_attempts) != 0:
                return 2
            workflow = SelectionWorkflow(JwzxClient(settings))
            workflow.open_page()
            workflow.status()
            consecutive_errors = 0
            continue
        except (URLError, TimeoutError) as error:
            consecutive_errors += 1
            logger.warning("第 %d 次网络异常（%d/5）：%s", checks, consecutive_errors, error)
            if consecutive_errors >= 5:
                raise RuntimeError("连续 5 次网络异常，已停止") from error

        if args.max_checks == 0 or checks < args.max_checks:
            time.sleep(args.interval)

    logger.info("达到最大监测次数，未完成选课")
    return 3


def run_login(settings: Settings, ocr_attempts: int) -> int:
    if ocr_attempts < 0:
        raise ValueError("ocr-attempts 不能小于 0")
    client = JwzxClient(settings)
    login_page = client.get_login_page()
    if login_page.status != 200 or 'name="j_username"' not in login_page.text:
        raise RuntimeError(
            f"登录页加载失败：HTTP {login_page.status}, Location={login_page.location!r}"
        )

    username = (
        getattr(settings, "username", "")
        or os.environ.get("JWZX_USERNAME", "")
        or SELECT_USERNAME
    )
    password = (
        getattr(settings, "password", "")
        or os.environ.get("JWZX_PASSWORD", "")
        or SELECT_PASSWORD
    )
    if not username:
        username = input("请输入学号：").strip()
    if not password:
        import getpass

        password = getpass.getpass("请输入密码：").strip()

    captcha_code = obtain_valid_captcha(
        client.fetch_captcha,
        client.validate_captcha,
        ocr_attempts=ocr_attempts,
        label="登录验证码",
    )
    if captcha_code is None:
        print("已取消登录")
        return 1
    result = client.login(username, password, captcha_code)
    if result.state == LoginState.SUCCESS:
        logger.info(result.detail)
        return 0
    if result.state == LoginState.BAD_CREDENTIALS:
        logger.error(result.detail)
        return 3
    logger.error(result.detail)
    return 2


def run_logout(settings: Settings) -> int:
    client = JwzxClient(settings)
    if client.logout():
        logger.info("已退出登录并清除本地会话")
        return 0
    logger.error("退出响应不符合已知抓包，本地会话未清除")
    return 2


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--epid", default="", help="教学班唯一标识，最精确")
    parser.add_argument("--course-code", default="", help="课程号，精确匹配")
    parser.add_argument("--name", default="", help="课程名称，支持部分匹配")
    parser.add_argument("--teacher", default="", help="教师姓名，支持部分匹配")
    parser.add_argument("--alias", default="", help="课程班别名，支持部分匹配")


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    _add_target_arguments(parser)
    parser.add_argument("--yes", action="store_true", help="跳过目标课程的 yes 确认")
    parser.add_argument(
        "--ocr-attempts",
        type=int,
        default=3,
        help="验证码自动识别并预校验次数，默认 3；0 表示直接手动输入",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="教务系统登录、查课与选课辅助脚本")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--trace-http", action="store_true", help="显示请求和响应诊断（隐藏敏感值）")
    parser.add_argument("--username", default="", help="学号（未提供则从配置、环境变量或交互输入读取）")
    parser.add_argument("--password", default="", help="密码（未提供则从配置、环境变量或交互输入读取）")

    subparsers = parser.add_subparsers(dest="command")

    login_p = subparsers.add_parser(
        "login",
        help="使用固定账号密码，自动识别验证码并保存会话",
    )
    login_p.add_argument(
        "--ocr-attempts",
        type=int,
        default=3,
        help="OCR 识别并预校验次数，默认 3；0 表示手动输入",
    )

    subparsers.add_parser("logout", help="退出登录并清除本地会话")

    subparsers.add_parser("check", help="执行单次检查")

    watch_p = subparsers.add_parser("watch", help="持续监测选课入口")
    watch_p.add_argument("--interval", type=float, default=None, help="检查间隔秒数，最小 5")
    watch_p.add_argument("--max-checks", type=int, default=0, help="轮询次数（0 为常驻）")

    courses_p = subparsers.add_parser("courses", aliases=["list"], help="查询待选课程与余量")
    _add_target_arguments(courses_p)

    subparsers.add_parser("selected", help="查询已选课程")

    select_p = subparsers.add_parser("select", help="有余量时单次选择指定教学班")
    _add_selection_arguments(select_p)

    grab_p = subparsers.add_parser(
        "grab",
        aliases=["rush"],
        help="持续监测指定教学班，有余量时完成验证码和单次提交",
    )
    _add_selection_arguments(grab_p)
    grab_p.add_argument("--interval", type=float, default=5.0, help="容量检查间隔秒数，最小 5")
    grab_p.add_argument("--max-checks", type=int, default=0, help="最大检查次数，0 为常驻")
    grab_p.add_argument(
        "--max-submit-attempts",
        type=int,
        default=3,
        help="因满员或系统繁忙导致失败时的最大提交次数，默认 3",
    )

    return parser


def run_menu(settings: Settings) -> int:
    while True:
        print("\n1. 登录  2. 查询待选课程  3. 查询已选课程  4. 监测并选课  5. 监测开放  6. 退出登录")
        print("q. 退出程序")
        choice = input("请选择：").strip().lower()
        if choice == "q":
            return 0
        try:
            if choice == "1":
                run_login(settings, 3)
            elif choice == "2":
                run_courses(
                    settings,
                    argparse.Namespace(
                        epid="", course_code="", name="", teacher="", alias=""
                    ),
                )
            elif choice == "3":
                run_selected(settings)
            elif choice == "4":
                epid = input("请输入目标教学班 epid（先用第 2 项查询）：").strip()
                args = argparse.Namespace(
                    epid=epid,
                    course_code="",
                    name="",
                    teacher="",
                    alias="",
                    yes=False,
                    ocr_attempts=3,
                    interval=5.0,
                    max_checks=0,
                    max_submit_attempts=3,
                )
                run_grab(settings, args)
            elif choice == "5":
                ProbeService.execute_watch_loop(settings, settings.poll_interval_seconds, 0)
            elif choice == "6":
                run_logout(settings)
            else:
                print("无效选择")
        except AuthRequiredError as error:
            logger.error("%s；请先选择 1 登录", error)
        except Exception as error:
            logger.error("操作失败: %s", error)


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        settings = Settings.load(args.config)
        if args.trace_http:
            settings = replace(settings, trace_http=True)
        if getattr(args, "username", ""):
            settings = replace(settings, username=args.username)
        if getattr(args, "password", ""):
            settings = replace(settings, password=args.password)

        if args.command is None:
            return run_menu(settings)

        if args.command == "login":
            return run_login(settings, args.ocr_attempts)

        if args.command == "logout":
            return run_logout(settings)

        if args.command == "check":
            client = JwzxClient(settings)
            client.require_session()
            res = client.check_open_state()
            logger.info("结果: [%s] %s", res.state.value, res.detail)
            return 0 if res.state == OpenState.CLOSED else 2

        if args.command == "watch":
            interval = settings.poll_interval_seconds if args.interval is None else args.interval
            return ProbeService.execute_watch_loop(settings, interval, args.max_checks)

        if args.command in {"courses", "list"}:
            return run_courses(settings, args)

        if args.command == "selected":
            return run_selected(settings)

        if args.command == "select":
            return run_select(settings, args)

        if args.command in {"grab", "rush"}:
            return run_grab(settings, args)

        return 1

    except KeyboardInterrupt:
        logger.info("用户终止操作")
        return 0
    except AuthRequiredError as err:
        logger.error("%s；请先运行 python .\\course_helper.py login", err)
        return 2
    except Exception as err:
        logger.error("运行失败: %s", err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
