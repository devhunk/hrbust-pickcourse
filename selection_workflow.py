"""教务系统选课接口工作流。

接口与字段来自 2026-09-15 的完整浏览器抓包。模块只负责单会话、串行请求；
不会并发提交，也不会把一次失败无限放大为请求洪泛。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit


class ResponseLike(Protocol):
    status: int
    body: bytes
    text: str
    location: str

    def header(self, name: str) -> str: ...


class ClientLike(Protocol):
    settings: Any

    def get(
        self,
        path: str,
        custom_timeout: float | None = None,
        referer: str | None = None,
    ) -> ResponseLike: ...

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
        custom_timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> ResponseLike: ...

    def require_session(self) -> None: ...


class SelectionError(RuntimeError):
    pass


class SelectionClosedError(SelectionError):
    pass


class SelectionSessionError(SelectionError):
    """服务器未确认选课会话；提示同时包含超时和在线人数两种可能。"""
    pass


class AuthRequiredError(SelectionError):
    pass


@dataclass(frozen=True)
class Course:
    epid: str
    cid: str
    course_code: str
    name: str
    sequence: str
    alias: str
    teacher: str
    classes: str
    property_code: str
    credit: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Course":
        return cls(
            epid=str(raw.get("epid", "")).strip(),
            cid=str(raw.get("cid", "")).strip(),
            course_code=str(raw.get("pcourseid", "")).strip(),
            name=str(raw.get("coursename", "")).strip(),
            sequence=str(raw.get("cseq", "")).strip(),
            alias=str(raw.get("alias", "")).strip(),
            teacher=str(raw.get("teachernames", "")).strip(),
            classes=str(raw.get("classes", "")).strip(),
            property_code=str(raw.get("prop", "")).strip(),
            credit=str(raw.get("credit", "")).strip(),
        )

    def display(self) -> str:
        teacher = self.teacher or "未标注"
        alias = self.alias or "无"
        return (
            f"epid={self.epid} | {self.name} | 课号={self.course_code} | "
            f"课序={self.sequence} | 教师={teacher} | 班别名={alias}"
        )


@dataclass(frozen=True)
class Capacity:
    epid: str
    selected: int
    remaining: int
    total: int


@dataclass(frozen=True)
class SelectionOutcome:
    success: bool
    verified: bool
    message: str
    status: int | None
    epid: str


def parse_json_payload(text: str) -> Any:
    """解析普通 JSON 及该站点使用的 jcallback(JSON) 包装。"""
    value = text.lstrip("\ufeff\r\n\t ").rstrip()
    match = re.fullmatch(r"jcallback\s*\((.*)\)\s*;?", value, re.S)
    if match:
        value = match.group(1).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        excerpt = re.sub(r"\s+", " ", value[:160])
        raise SelectionError(f"接口返回无法解析为 JSON：{excerpt!r}") from error


def _int_status(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_session_message(value: str) -> bool:
    return any(marker in value for marker in ("最大在线人数", "选课超时", "人数过多"))


class _NavigationLinks(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        field = {"frame": "src", "iframe": "src", "a": "href", "form": "action"}.get(tag)
        if field:
            value = dict(attrs).get(field)
            if value:
                self.links.append((tag, value))


class SelectionWorkflow:
    PAGE = "/academic/manager/electcourse/elective.do"
    INFO = "/academic/manager/electcourse/electiveMgs.do"
    STATUS = "/academic/manager/electcourse/electiveStatus.do"
    CAPACITY = "/academic/manager/electcourse/findCoursecapabilityAmount.do"
    SELECTED = "/academic/manager/electcourse/findElectiveFinshCourse.do"
    ADD = "/academic/manager/electcourse/electiveSelectCourseAdd.do"
    CAPTCHA = "/academic/manager/electcourse/getCaptcha.do"
    CAPTCHA_CHECK = "/academic/manager/electcourse/checkCaptcha.do"
    TMP_DIR = "/academic/tmpfile"

    AJAX_HEADERS = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    DOCUMENT_HEADERS = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self, client: ClientLike) -> None:
        self.client = client
        self.referer = urljoin(client.settings.base_url, self.PAGE) + "?"
        self._student_user_id: str | None = None
        self._nonce = int(time.time() * 1000)
        self.last_response: ResponseLike | None = None

    def _cache_buster(self) -> int:
        # 与页面 jQuery cache:false 的递增参数一致，不复用抓包中的旧值。
        self._nonce += 1
        return self._nonce

    def _same_origin_url(self, value: str, base: str) -> str:
        target = urljoin(base, value)
        expected, actual = urlsplit(self.client.settings.base_url), urlsplit(target)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise SelectionError("选课入口跳到了其他站点，无法按现有抓包继续")
        return target

    def _document(self, path: str, referer: str) -> tuple[str, ResponseLike]:
        current = self._same_origin_url(path, self.client.settings.base_url)
        for _ in range(6):
            response = self.client.request(
                current, referer=referer, headers=self.DOCUMENT_HEADERS
            )
            self.last_response = response
            self._raise_for_auth(response)
            if "errorunopen.html" in (current + response.location).lower():
                raise SelectionClosedError("当前不在选课开放时间")
            if response.status in {301, 302, 303, 307, 308} and response.location:
                # HTTP 重定向沿用导航发起页；新的表单导航才改变 Referer。
                current = self._same_origin_url(response.location, current)
                continue
            if response.status != 200:
                raise SelectionError(f"导航请求失败：{urlsplit(current).path} HTTP {response.status}")
            return current, response
        raise SelectionError("选课导航重定向超过 5 次")

    def _json_response(self, response: ResponseLike, endpoint: str) -> Any:
        self.last_response = response
        SelectionWorkflow._raise_for_auth(response)
        if response.status != 200:
            raise SelectionError(f"{endpoint} 请求失败：HTTP {response.status}")
        payload = parse_json_payload(response.text)
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        if _is_session_message(str(message)) and _int_status(payload.get("status")) != 0:
            raise SelectionSessionError(
                f"{endpoint}：{message}（服务端未确认当前选课会话，请重新进入；不能仅据此判断人数已满）"
            )
        return payload

    @staticmethod
    def _raise_for_auth(response: ResponseLike) -> None:
        location = response.location.lower()
        body = response.text.lower()
        if response.status in {401, 403} or (
            response.status in {301, 302, 303, 307, 308}
            and ("login" in location or "index.jsp" in location)
        ):
            raise AuthRequiredError("登录态已失效，请重新登录")
        if 'name="j_password"' in body or 'name="j_username"' in body:
            raise AuthRequiredError("接口返回登录页，请重新登录")

    def open_page(self) -> str:
        self.client.require_session()
        base = self.client.settings.base_url
        frames_url, frames = self._document(
            "/academic/frameset_index.jsp", urljoin(base, "/academic/index_new.jsp")
        )
        menu_link = next(
            (url for tag, url in _NavigationLinks(frames.text).links
             if tag in {"frame", "iframe"}
             and urlsplit(url).path.rsplit("/", 1)[-1] == "listLeft.do"),
            None,
        )
        if not menu_link:
            raise SelectionError("主框架缺少 listLeft.do 菜单入口")
        menu_url, menu = self._document(urljoin(frames_url, menu_link), frames_url)
        entry = next(
            (url for tag, url in _NavigationLinks(menu.text).links
             if tag == "a" and urlsplit(url).path.endswith("accessModule.do")
             and parse_qs(urlsplit(url).query).get("moduleId") == ["2050"]),
            None,
        )
        if not entry:
            raise SelectionError("当前菜单没有学生选课入口（moduleId=2050）")
        jump_url, jump = self._document(urljoin(menu_url, entry), menu_url)
        if urlsplit(jump_url).path == self.PAGE:
            page_url, response = jump_url, jump
        else:
            form_action = next(
                (url for tag, url in _NavigationLinks(jump.text).links
                 if tag == "form" and urlsplit(urljoin(jump_url, url)).path == self.PAGE),
                None,
            )
            if not form_action:
                raise SelectionError("选课跳转页缺少 elective.do 表单入口")
            page_url, response = self._document(urljoin(jump_url, form_action), jump_url)
        self.referer = page_url if "?" in page_url else page_url + "?"
        if "electiveStatus.do" not in response.text:
            raise SelectionError("选课页面结构与抓包不一致")
        match = re.search(r"var\s+student\s*=\s*(\{.*?\})\s*;", response.text, re.S)
        if not match:
            raise SelectionError("选课页面缺少 student.userid，无法定位个人课程 JSON")
        try:
            student = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise SelectionError("选课页面中的 student 数据无法解析") from error
        user_id = str(student.get("userid", "")).strip()
        if not user_id.isdigit():
            raise SelectionError("选课页面中的 student.userid 无效")
        self._student_user_id = user_id
        info = self.client.request(
            f"{self.INFO}?_={self._cache_buster()}",
            referer=self.referer,
            headers=self.AJAX_HEADERS,
        )
        if not isinstance(self._json_response(info, "electiveMgs.do"), dict):
            raise SelectionError("选课信息返回类型异常")
        return user_id

    def status(self) -> dict[str, Any]:
        response = self.client.request(
            f"{self.STATUS}?_={self._cache_buster()}",
            referer=self.referer,
            headers=self.AJAX_HEADERS,
        )
        payload = self._json_response(response, "electiveStatus.do")
        if not isinstance(payload, dict):
            raise SelectionError("选课状态返回类型异常")
        if _int_status(payload.get("status")) == -1:
            raise SelectionClosedError(str(payload.get("message") or "当前不可选课"))
        if _int_status(payload.get("status")) != 0:
            raise SelectionError(f"未知选课状态：{payload!r}")
        return payload

    def list_courses(self) -> list[Course]:
        user_id = self._student_user_id or self.open_page()
        response = self.client.request(
            f"{self.TMP_DIR}/s{user_id}.json?_={self._cache_buster()}",
            referer=self.referer,
            headers={
                **self.AJAX_HEADERS,
                "Accept": (
                    "text/javascript, application/javascript, application/ecmascript, "
                    "application/x-ecmascript, */*; q=0.01"
                ),
                "Content-Type": "text/json,charset=utf-8",
            },
        )
        self._raise_for_auth(response)
        if response.status != 200:
            raise SelectionError(f"个人待选课程请求失败：HTTP {response.status}")
        payload = parse_json_payload(response.text)
        if not isinstance(payload, list):
            raise SelectionError("个人待选课程返回类型异常")
        courses = [Course.from_dict(item) for item in payload if isinstance(item, dict)]
        return [course for course in courses if course.epid]

    @staticmethod
    def filter_courses(
        courses: list[Course],
        *,
        epid: str = "",
        course_code: str = "",
        name: str = "",
        teacher: str = "",
        alias: str = "",
    ) -> list[Course]:
        exact_epid = epid.strip()
        exact_code = course_code.strip().casefold()
        name_part = name.strip().casefold()
        teacher_part = teacher.strip().casefold()
        alias_part = alias.strip().casefold()

        def matches(course: Course) -> bool:
            return (
                (not exact_epid or course.epid == exact_epid)
                and (not exact_code or course.course_code.casefold() == exact_code)
                and (not name_part or name_part in course.name.casefold())
                and (not teacher_part or teacher_part in course.teacher.casefold())
                and (not alias_part or alias_part in course.alias.casefold())
            )

        return [course for course in courses if matches(course)]

    def capacities(self, epids: list[str]) -> dict[str, Capacity]:
        clean = list(dict.fromkeys(str(epid).strip() for epid in epids if str(epid).strip()))
        if not clean:
            return {}
        data = urlencode([("epid", epid) for epid in clean]).encode("ascii")
        response = self.client.request(
            self.CAPACITY,
            method="POST",
            data=data,
            referer=self.referer,
            headers={
                **self.AJAX_HEADERS,
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self.client.settings.base_url,
            },
        )
        self._raise_for_auth(response)
        if response.status != 200:
            raise SelectionError(f"课程容量请求失败：HTTP {response.status}")
        payload = parse_json_payload(response.text)
        if not isinstance(payload, list):
            raise SelectionError("课程容量返回类型异常")
        result: dict[str, Capacity] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            epid = str(item.get("epid", "")).strip()
            if not epid:
                continue
            try:
                result[epid] = Capacity(
                    epid=epid,
                    selected=int(item.get("amount", 0)),
                    remaining=int(item.get("remainCapability", 0)),
                    total=int(item.get("courseCapability", 0)),
                )
            except (TypeError, ValueError) as error:
                raise SelectionError(f"课程 {epid} 的容量字段异常") from error
        return result

    def selected_courses(self) -> list[dict[str, Any]]:
        response = self.client.request(
            f"{self.SELECTED}?_={self._cache_buster()}",
            referer=self.referer,
            headers=self.AJAX_HEADERS,
        )
        payload = self._json_response(response, "findElectiveFinshCourse.do")
        if not isinstance(payload, dict) or _int_status(payload.get("status")) != 0:
            message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            raise SelectionError(f"已选课程返回异常：{message or payload!r}")
        datas = payload.get("datas")
        rows = datas.get("scheduleScoreList", []) if isinstance(datas, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    def is_selected(self, epid: str) -> bool:
        wanted = str(epid)
        return any(str(row.get("epid", "")) == wanted for row in self.selected_courses())

    def fetch_captcha(self) -> Path:
        response = self.client.request(
            f"{self.CAPTCHA}?{time.time() % 1:.16f}",
            referer=self.referer,
            headers={"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"},
        )
        self._raise_for_auth(response)
        if response.status != 200 or not response.header("Content-Type").lower().startswith(
            "image/jpeg"
        ):
            raise SelectionError(
                f"获取选课验证码失败：HTTP {response.status}, {response.header('Content-Type')}"
            )
        base = self.client.settings.captcha_file
        base = base if base.is_absolute() else Path(__file__).parent / base
        target = base.with_name("elective_captcha.jpg")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.body)
        return target

    def validate_captcha(self, code: str) -> bool:
        query = urlencode({"captchaCode": code})
        response = self.client.request(
            f"{self.CAPTCHA_CHECK}?{query}",
            method="POST",
            referer=self.referer,
            headers={
                **self.AJAX_HEADERS,
                "Accept": "text/plain, */*; q=0.01",
                "Origin": self.client.settings.base_url,
            },
        )
        self._raise_for_auth(response)
        value = response.text.strip().lower()
        if response.status != 200 or value not in {"true", "false"}:
            raise SelectionError(f"选课验证码校验异常：HTTP {response.status}, {value[:80]!r}")
        return value == "true"

    def submit_once(self, course: Course) -> SelectionOutcome:
        query = urlencode({"epid": course.epid, "_": self._cache_buster()})
        response = self.client.request(
            f"{self.ADD}?{query}",
            referer=self.referer,
            headers=self.AJAX_HEADERS,
        )
        self._raise_for_auth(response)
        if response.status != 200:
            return SelectionOutcome(False, False, f"提交请求失败：HTTP {response.status}", None, course.epid)
        payload = parse_json_payload(response.text)
        if not isinstance(payload, dict):
            raise SelectionError("选课提交返回类型异常")
        top_status = _int_status(payload.get("status"))
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            if top_status == 0:
                raise SelectionError("提交响应缺少 result，无法确认结果；请在网页核实后再操作")
            raw_result = {}
        item_status = _int_status(raw_result.get("status"))
        message = str(raw_result.get("message") or payload.get("message") or "选课返回无说明")

        accepted = top_status == 0 and item_status == 0
        if accepted and str(raw_result.get("epid", "")) != course.epid:
            raise SelectionError("提交响应的 epid 与目标不符，请在网页核实结果")
        return SelectionOutcome(
            success=accepted,
            verified=False,
            message=message,
            status=item_status if top_status == 0 else top_status,
            epid=course.epid,
        )
