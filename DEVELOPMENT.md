# PickCourse 二次开发指南

本文介绍项目结构、核心协议、测试方法和扩展规范。

## 项目结构

```text
PickCouse/
├── course_helper.py       # 登录、交互菜单、监测与选课调度
├── selection_workflow.py  # 选课协议和业务流程
├── jwzx_portal.py         # 课表、成绩、考试等只读查询
├── config.example.json    # 配置示例
├── tests/                 # 离线测试与脱敏抓包
├── GetCaptcha/            # 验证码获取测试
├── ocr/                   # OCR 测试
└── 验证码集/              # 验证码样本工具
```

核心组件：

- `JwzxClient`：HTTP 请求、Cookie、登录、验证码和异常处理。
- `SelectionWorkflow`：选课导航、课程解析、容量查询和提交。
- `PortalClient`：教务模块访问、表格解析和数据导出。

## 核心流程

### 登录

```text
login.jsp
→ getCaptcha.do
→ checkCaptcha.do
→ j_acegi_security_check
→ 保存 Cookie
```

选课和综合教务分别使用：

```text
state/session.cookies.txt
state/portal_session.cookies.txt
```

### 选课导航

必须完整执行导航并获取实时 `randomString`：

```text
frameset_index.jsp
→ listLeft.do
→ accessModule.do
→ jumppage.jsp
→ elective.do
→ electiveMgs.do
→ electiveStatus.do
```

选课页面会话约为 5 分钟。出现“选课超时或者达到最大在线人数”时，应重新执行完整导航。

### 查询与提交

- 容量接口使用一次 POST 批量提交多个 `epid`。
- 保持单会话、串行请求。
- 发现余量并通过验证码校验后，只提交一次选课请求。
- 不要通过高频或并发请求增加服务器压力。

## 开发环境

要求 Python 3.10 及以上版本。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
```

账号可配置在 `config.json`，也可使用环境变量：

```powershell
$env:JWZX_USERNAME="学号"
$env:JWZX_PASSWORD="密码"
```

`config.json` 已加入 `.gitignore`，禁止提交真实凭据。

## 测试

运行全部离线测试：

```powershell
py -3 -m unittest discover -s tests -v
```

`tests/test_capture_contract.py` 使用本地服务器回放脱敏抓包，检查：

- 请求方法和路径
- 参数与 POST 正文
- `Referer`、`Origin`、`Accept`
- Cookie 生命周期
- 压缩响应处理
- 状态判断和异常处理

测试不会向学校服务器提交课程。

## 功能扩展

### 新增教务查询

在 `PortalClient` 中：

1. 使用 `open_module(moduleId)` 打开模块。
2. 请求对应接口。
3. 使用 `extract_tables()` 解析表格。
4. 为新功能补充离线测试。

### 接入消息通知

可在状态变化或提交完成后调用 Webhook。地址应从环境变量读取，通知失败不得中断主流程，也不得发送密码、Cookie 等敏感信息。

### 替换验证码 OCR

修改 `recognize_captcha_digits()` 即可接入其他识别方式，但应保留在线预校验流程。验证码长度不能固定为 4 位或 6 位。

### 多目标监测

应通过容量接口一次查询多个 `epid`，再按配置顺序处理有余量的课程，禁止为每门课程创建高频独立循环。

## 抓包与脱敏

真实抓包不得提交到 Git，尤其禁止包含：

- 姓名、学号和身份证号
- 密码、Cookie 和 Session
- 成绩、住址和其他个人信息

测试夹具必须使用虚拟数据，例如：

```text
学号：2026000001
姓名：测试用户
Cookie：replay-session
```

提交前检查：

```powershell
git status -u
```

## 开发规范

- 优先使用 Python 标准库，避免不必要的依赖。
- 新增接口尽量添加类型注解。
- 协议变更必须同步更新离线测试。
- 提交前确保全部测试通过。
- 推荐使用 `feat:`、`fix:`、`docs:` 等提交前缀。