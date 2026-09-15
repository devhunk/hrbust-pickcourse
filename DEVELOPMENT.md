# PickCourse 二次开发指南 (Developer Guide)

本文档旨在帮助开发者快速理解本项目的设计架构、通信协议细节、测试规范以及常见功能的二次开发扩展方法。

---

## 目录

- [一、项目架构与模块分工](#一项目架构与模块分工)
- [二、核心通信协议与业务流](#二核心通信协议与业务流)
- [三、开发环境搭建](#三开发环境搭建)
- [四、测试体系与离线回放机制](#四测试体系与离线回放机制)
- [五、二次开发与扩展示例](#五二次开发与扩展示例)
  - [1. 扩展新的教务只读查询模块](#1-扩展新的教务只读查询模块)
  - [2. 接入抢课/状态变化消息通知 (钉钉/微信/Telegram)](#2-接入抢课状态变化消息通知-钉钉微信telegram)
  - [3. 替换或增强验证码识别引擎](#3-替换或增强验证码识别引擎)
  - [4. 多目标课程轮询与优先级抢课](#4-多目标课程轮询与优先级抢课)
- [六、协议抓包与安全脱敏规范](#六协议抓包与安全脱敏规范)
- [七、代码风格与 PR 规范](#七代码风格与-pr-规范)

---

## 一、项目架构与模块分工

本项目采用 **分层解耦** 架构，将底层 HTTP 通信、业务工作流、高层客户端交互以及测试夹具严格分离：

```
PickCouse/
├── course_helper.py       # 选课交互入口、Session 管理、容量监测与抢课调度引擎
├── selection_workflow.py  # 纯净的选课协议工作流（与具体网络传输层解耦）
├── jwzx_portal.py         # 教务综合服务客户端（只读模块：课表、成绩、学籍、计划等）
├── config.example.json    # 配置文件模版（包括接口端点、轮询间隔、超时等）
├── requirements.txt       # 项目核心依赖清单
├── DEVELOPMENT.md         # 二次开发指南（本文档）
├── README.md              # 用户说明与快速上手
├── tests/                 # 离线自动化测试套件
│   ├── fixtures/captures/ # 脱敏离线回放真实抓包数据（用于 wire-level 契约测试）
│   ├── test_capture_contract.py  # 真实报文结构契约测试
│   ├── test_course_helper.py     # 选课引擎与 CLI 交互测试
│   └── test_jwzx_portal.py       # 综合客户端与 HTML 解析器测试
├── GetCaptcha/            # 验证码获取独立测试脚本
├── ocr/                   # ddddocr 识别独立验证脚本
└── 验证码集/              # 验证码样本批量爬取工具（图片集已默认 gitignore）
```

### 核心类设计与职责

- **`course_helper.JwzxClient`**：底层传输客户端。基于 `urllib.request` 封装，自动管理 `MozillaCookieJar`（支持会话持久化与隔离）、处理自动重定向、多格式解压（gzip 与 deflate 流）、异常诊断以及登录和验证码获取。
- **`selection_workflow.SelectionWorkflow`**：选课核心业务流。只依赖协议抽象（`ClientLike`），处理选课框架导航、动态 `randomString` 提取、课程列表解析、批量余量查询与单次选课提交。
- **`jwzx_portal.PortalClient`**：综合教务业务客户端。基于动态菜单 `moduleId` 的页面反射与表格解析（`TableParser`），抓取并导出课表、成绩、校历、考试安排与学籍档案。

---

## 二、核心通信协议与业务流

### 1. 认证与会话管理流 (Authentication)

1. **登录页与初始会话**：
   - 请求 `GET /academic/common/security/login.jsp`，服务端 Set-Cookie 分配匿名 `JSESSIONID`。
2. **验证码交互**：
   - 请求 `GET /academic/getCaptcha.do` 获取验证码图片；
   - 提交验证码在线预校验 `POST /academic/checkCaptcha.do`。
3. **表单安全提交**：
   - 提交 `POST /academic/j_acegi_security_check`（参数：`j_username`, `j_password`, `j_captcha`）；
   - 登录成功后服务端将该 `JSESSIONID` 标记为已认证；
   - `JwzxClient` 将 Cookie 自动持久化保存到 `state/session.cookies.txt`（综合门户客户端保存在 `state/portal_session.cookies.txt`，两套会话物理隔离）。

### 2. 选课导航与动态令牌机制 (Dynamic Token Flow)

教务系统不允许跨步直接请求选课接口，必须严格按照以下链条进行页面导航，提取服务端动态生成的 `randomString`：

```
frameset_index.jsp (获取主框架)
  └── listLeft.do (获取左侧功能树，解析包含 randomString 的学生选课链接)
        └── accessModule.do?moduleId=2050&groupId=&randomString=XXX (302 跳转)
              └── jumppage.jsp (选课中转跳转页)
                    └── elective.do?randomString=XXX (选课主界面)
                          ├── electiveMgs.do (初始化选课环境)
                          └── electiveStatus.do (确认系统开放状态)
```

> **重要机制**：
> - 选课界面内嵌脚本声明 `selectCourseTime = "5.0"`（会话有效期为 5 分钟），单靠常规接口轮询**不会**自动延长选课会话。
> - 服务端返回 `选课超时或者达到最大在线人数` 时，代表选课层临时会话已断开，必须重新执行上述完整导航链。

### 3. 选课操作与防刷限流设计

- **批量余量查询**：调用 `findSelectGroupAllCapacity.do`，使用重复的 `epid=123&epid=456` 参数单次 POST 查询，严禁单课高频循环请求。
- **单会话串行单次提交**：本项目严格坚持**单会话串行请求**，在余量大于 0 且完成选课验证码校验后，只发送**单次**提交请求，避免洪泛请求导致学校封禁 IP 或学生账号。

---

## 三、开发环境搭建

### 1. 环境需求

- **Python 3.10+**（推荐 Python 3.11 ~ 3.14）
- 操作系统：Windows / Linux / macOS

### 2. 克隆与初始化

```bash
git clone https://github.com/YOUR_USERNAME/PickCouse.git
cd PickCouse

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境 (Windows PowerShell)
.venv\Scripts\Activate.ps1
# 或 (Linux/macOS)
source .venv/bin/activate

# 安装开发依赖
pip install -r requirements.txt
```

### 3. 配置测试凭据

复制模版配置文件：
```bash
cp config.example.json config.json
```
在 `config.json` 中配置您的测试学号和密码（`config.json` 已被 `.gitignore` 保护，不会被 Git 追踪）：
```json
{
  "username": "YOUR_STUDENT_ID",
  "password": "YOUR_PASSWORD"
}
```
也可以通过环境变量配置：
```bash
# Windows PowerShell
$env:JWZX_USERNAME="YOUR_STUDENT_ID"
$env:JWZX_PASSWORD="YOUR_PASSWORD"

# Linux / macOS
export JWZX_USERNAME="YOUR_STUDENT_ID"
export JWZX_PASSWORD="YOUR_PASSWORD"
```

---

## 四、测试体系与离线回放机制

为保证在假期、校外离线、教务系统关闭时仍能进行持续集成与代码重构，本项目实现了 **Wire-level 抓包契约回放测试**。

### 1. 运行所有测试

```bash
py -3 -m unittest discover -s tests -v
```

目前覆盖 15 个用例：
- `test_actual_capture_navigation_headers_parameters_and_submit`: 启动本机测试 HTTP 服务器，回放 13 组脱敏报文，逐字段校验请求头（Referer、Origin、Accept）、参数格式及解压缩；
- `test_login_rotates_and_persists_cookie_then_logout_clears_it`: 校验 Cookie 轮转与持久化；
- `test_status_does_not_report_open_from_html_alone`: 校验防假开放逻辑；
- `test_watch_retries_network_error_and_stops_on_change`: 校验监控重试与容错。

### 2. 契约测试原理 (`tests/test_capture_contract.py`)

契约测试使用 `tests/fixtures/captures/` 下脱敏的请求和响应文本驱动 `ThreadingHTTPServer`。所有请求中的动态参数（如时间戳 `_`）被容差解析，固定参数与请求头严格对齐抓包事实。

---

## 五、二次开发与扩展示例

### 1. 扩展新的教务只读查询模块

`jwzx_portal.py` 内部实现了 `PortalClient.open_module(moduleId)` 与 `extract_tables(source)`，可轻松接入其他教务模块。

例如，需要新增“查询空闲教室”功能（假设 moduleId 为 1234）：

```python
# 在 jwzx_portal.py 中为 PortalClient 添加方法：
def empty_classrooms(self, campus: str = "南区") -> list[list[str]]:
    # 1. 打开指定功能模块，底层自动完成框架和动态菜单的 Referer 导航
    page = self.open_module(1234)
    
    # 2. 发起特定查询请求
    query_url = self.absolute_url("/academic/manager/coursearrange/queryEmptyRoom.do")
    res = self.http.request(query_url, method="POST", data=f"campus={campus}".encode())
    
    # 3. 使用内置的 extract_tables 解析 HTML 表格
    tables = extract_tables(res.text)
    return tables[0] if tables else []
```

### 2. 接入抢课/状态变化消息通知 (钉钉/微信/Telegram)

当监控到选课开放或成功抢到目标课程时，可以添加外部 Webhook 推送。

在 `selection_workflow.py` 或 `course_helper.py` 的 `run_grab` 中插入通知逻辑：

```python
import json
import urllib.request
import os

def send_notification(title: str, content: str) -> None:
    webhook_url = os.environ.get("NOTIFY_WEBHOOK_URL", "")
    if not webhook_url:
        return
    
    # 以飞书/钉钉 Webhook 格式为例：
    payload = {
        "msg_type": "text",
        "content": {"text": f"【选课通知】{title}\n{content}"}
    }
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning("推送通知失败: %s", e)
```

在 `run_grab` 成功提交后调用：
```python
if outcome.success:
    send_notification("抢课成功！", f"课程：{course.name} ({course.alias}) 已成功选入！")
```

### 3. 替换或增强验证码识别引擎

当前验证码识别逻辑封装在 `course_helper.py` 的 `recognize_captcha_digits` 函数中：

```python
def recognize_captcha_digits(image_path: Path) -> str | None:
    # 默认使用项目现有的 ddddocr 识别 4 位或 6 位纯数字验证码
    ...
```

**二次开发建议**：
1. **训练专用小模型**：利用 `验证码集/get_captcha.py` 采集样本打标，训练轻量 ONNX 模型替换；
2. **接入三方打码平台**：在函数内增加 HTTP 接口调用作为 OCR 识别失败后的降级备选；
3. **保留预校验机制**：保留原有的 `validate_code(code)` 预校验，只有在线校验通过的验证码才进行选课提交，节省宝贵的提交机会。

### 4. 多目标课程轮询与优先级抢课

当前 `run_grab` 针对单个教学班。如需支持“多选一”（哪个有名额优先抢哪个）：

```python
def run_multi_grab(settings: Settings, epids: list[str]) -> None:
    workflow = SelectionWorkflow(JwzxClient(settings))
    workflow.open_page()
    
    while True:
        # 批量获取全部目标课程实时余量（一次 POST，减少请求频次）
        cap_map = workflow.capacities(epids)
        for epid in epids:
            cap = cap_map.get(epid)
            if cap and cap.remaining > 0:
                logger.info("发现余量！目标 epid: %s，余量: %d", epid, cap.remaining)
                # 识别验证码并提交该课程
                # ...
                return
        time.sleep(settings.poll_interval_seconds)
```

---

## 六、协议抓包与安全脱敏规范

若教务系统后续升级改版导致接口变动，开发者在进行逆向分析与抓包时，**必须严格遵守以下脱敏守则**：

1. **绝对禁止将含个人数据的抓包提交进 Git**：
   - `origin/` 目录已永久被 `.gitignore` 排除。
   - 不得将含有个人真实姓名、学号、身份证号、家庭住址、成绩单、真实 Session Cookie 的抓包文件提交到任何分支。
2. **新增契约测试夹具的脱敏要求**：
   - 提取到 `tests/fixtures/captures/` 的测试用例，必须使用脚本对报文进行清洗：
     - 学号统一替换为 `2026000001` 等虚拟学号；
     - 姓名统一替换为 `测试用户` 或 `张三`；
     - 身份证/密码统一替换为 `TestPass123!`；
     - Cookie 统一替换为虚拟占位符（如 `replay-session`）。
3. **提交前自检命令**：
   在提交 PR 之前，在终端运行全库敏感词检索：
   ```powershell
   git status -u
   # 确保无未忽略的本地状态、缓存或截图
   ```

---

## 七、代码风格与 PR 规范

1. **标准库优先**：核心业务与网络协议层除 `ddddocr` 与可选的 `requests` 外，优先使用 Python 标准库（`urllib`, `dataclasses`, `http.cookiejar`），保持轻量与环境兼容性。
2. **类型注解**：新编写的模块与接口尽量添加类型提示（Type Annotations）。
3. **测试驱动**：所有修改在提交前必须确保通过本地离线测试：
   ```bash
   py -3 -m unittest discover -s tests -v
   ```
4. **提交信息规范**：推荐使用 Conventional Commits 格式，例如：
   - `feat: add empty classroom query module`
   - `fix: correct referer url encoding in electiveStatus`
   - `docs: update development guide`
