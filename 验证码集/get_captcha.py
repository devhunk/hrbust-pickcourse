#!/usr/bin/env python3
"""按固定间隔下载教务系统验证码图片，用于构建图片数据集。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


CAPTCHA_URL = "http://jwzx.hrbust.edu.cn/academic/getCaptcha.do"
LOGIN_URL = "http://jwzx.hrbust.edu.cn/academic/common/security/login.jsp"
SESSION_ENV = "JWZX_JSESSIONID"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "图片"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量下载教务系统验证码图片")
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=rf"保存目录，默认：{DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="两次请求的间隔秒数，默认 1 秒",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="下载数量；0 表示持续下载，直到按 Ctrl+C",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单次请求超时秒数，默认 10 秒",
    )
    return parser.parse_args()


def make_request(session_id: str) -> Request:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": LOGIN_URL,
    }
    if session_id:
        headers["Cookie"] = f"JSESSIONID={session_id}"
    return Request(CAPTCHA_URL, headers=headers, method="GET")


def download_captcha(opener, output_dir: Path, session_id: str, timeout: float, index: int) -> Path:
    with opener.open(make_request(session_id), timeout=timeout) as response:
        content_type = response.headers.get_content_type().lower()
        image_data = response.read()

        if response.status != 200:
            raise RuntimeError(f"HTTP 状态码：{response.status}")
        if content_type != "image/jpeg":
            raise RuntimeError(f"响应不是 JPEG 图片，Content-Type：{content_type}")
        if not image_data.startswith(b"\xff\xd8"):
            raise RuntimeError("响应内容不是有效的 JPEG 图片")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    output = output_dir / f"captcha_{timestamp}_{index:06d}.jpg"
    temporary = output.with_suffix(".tmp")
    try:
        temporary.write_bytes(image_data)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        print("错误：--interval 必须大于 0", file=sys.stderr)
        return 2
    if args.count < 0:
        print("错误：--count 不能小于 0", file=sys.stderr)
        return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    session_id = os.environ.get(SESSION_ENV, "").strip()
    if not session_id:
        session_id = input("请输入当前 JSESSIONID：").strip()
    if not session_id:
        print("错误：JSESSIONID 不能为空", file=sys.stderr)
        return 2
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    print(f"保存目录：{output_dir}")
    print(f"获取频率：每 {args.interval:g} 秒 1 张；按 Ctrl+C 停止")

    downloaded = 0
    attempted = 0
    next_request_at = time.monotonic()
    try:
        while args.count == 0 or attempted < args.count:
            remaining = next_request_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

            attempted += 1
            index = attempted
            try:
                saved_path = download_captcha(
                    opener,
                    output_dir,
                    session_id,
                    args.timeout,
                    index,
                )
            except HTTPError as exc:
                print(f"[{index}] 请求失败：HTTP {exc.code}", file=sys.stderr)
            except URLError as exc:
                print(f"[{index}] 连接失败：{exc.reason}", file=sys.stderr)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[{index}] 获取失败：{exc}", file=sys.stderr)
            else:
                downloaded += 1
                print(f"[{downloaded}] {saved_path.name}")

            next_request_at = time.monotonic() + args.interval
    except KeyboardInterrupt:
        print(f"\n已停止，共保存 {downloaded} 张验证码。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
