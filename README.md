# 选课辅助与教务在线客户端

基于真实抓包协议实现的教务在线及选课辅助工具，支持 Cookie 自动维护、课程查询、容量监测、选课验证码自动识别与抢课提交。交互菜单执行完一项操作后会返回菜单，输入 `q` 退出程序。

## 配置凭据说明

项目**不包含**任何硬编码账号密码，凭据支持以下多种安全配置方式（按优先级）：

1. **命令行参数**：`--username` 与 `--password`；
2. **本地配置文件**：复制 `config.example.json` 为 `config.json`（该文件已加入 `.gitignore`，不会被提交），并在其中填入 `username` 和 `password`；
3. **环境变量**：设置环境变量 `JWZX_USERNAME` 与 `JWZX_PASSWORD`；
4. **终端交互**：未配置时，运行登录会自动提示输入学号，并使用隐式密码输入（无回显）。

## 依赖安装

```powershell
pip install -r requirements.txt # 或 pip install requests ddddocr
```

## 教务在线综合客户端

`jwzx_portal.py` 提供综合教务信息查询与状态监测：

```powershell
py -3 .\jwzx_portal.py
```

综合客户端使用 `state/portal_session.cookies.txt`，与选课脚本的登录会话相互隔离。

已实现的请求功能包括：

- 本学期课表、个人教学计划、校历
- 个人成绩、课程成绩、等级考试成绩
- 考试安排
- 课程目录查询
- 学籍信息和照片
- 学生选课入口检查与低频监测
- 按 `moduleId` 获取其他菜单模块

持续监测选课开放状态：

```powershell
py -3 .\jwzx_portal.py watch --interval 30
```

监测会持续运行到入口状态变化或按 `Ctrl+C`；短暂网络错误会自动重试，连续失败 5 次才停止。

获取结果会同时保存为原始 HTML 和表格 JSON，输出目录为 `exports`。

## 选课辅助脚本用法

配置文件可选；默认配置已对应当前抓包。直接运行会显示交互菜单：

```powershell
py .\course_helper.py
```

支持 `ddddocr` 自动识别登录验证码和选课验证码并在线预校验；连续失败后，图片分别保存到 `state/captcha.jpg` 或 `state/elective_captcha.jpg` 供手动输入。登录成功后 Cookie 自动保存到 `state/session.cookies.txt`。

登录：

```powershell
py .\course_helper.py login
```

查询待选课程和实时余量，可按课程名、教师、课程号或班别名筛选：

```powershell
py .\course_helper.py courses --name "大学英语"
```

输出中的 `epid` 是教学班唯一标识。确认目标后持续监测，有余量即完成验证码和单次提交：

```powershell
py .\course_helper.py grab --epid 目标epid
```

也可以直接筛选，但筛选结果必须唯一：

```powershell
py .\course_helper.py grab --name "课程名称" --teacher "教师姓名" --alias "班别名"
```

单次选课使用 `select`；查看已选课程使用 `selected`：

```powershell
py .\course_helper.py select --epid 目标epid
py .\course_helper.py selected
```

单次检查选课入口：

```powershell
py -3 .\course_helper.py check
```

持续监测入口开放状态（默认每 30 秒一次）：

```powershell
py .\course_helper.py watch
```

退出登录并删除本地会话：

```powershell
py .\course_helper.py logout
```

脚本不会打印密码或 `JSESSIONID`。会话文件和验证码图片已加入 `.gitignore`。`watch` 和 `grab` 检测到登录失效时，会尝试重新登录，连续失效 3 次停止。网页抓包中的选课倒计时为 5 分钟，不能假定持续查询会延长选课会话；服务端报告选课会话不可用时会保留具体接口和原文，返回菜单重新进入。

`grab` 默认每 5 秒查询一次容量，只串行提交。验证码校验通过后只调用选课提交接口，不自动查询已选列表；以提交响应中的 `status`、`result.status` 和目标 `epid` 确认接口结果。接口返回成功不代表已进行独立回查。

## 抓包对照与请求诊断

选课相关操作先读取主框架和动态菜单，再按 `accessModule.do → jumppage.jsp → elective.do` 导航，保留服务端返回的 `randomString`，然后读取页面初始化信息和选课状态。页面导航的 `Referer` 为跳转页，后续接口的 `Referer` 为带尾部 `?` 的选课页地址；验证码 POST 带 `Origin` 且正文为空。

“选课超时或者达到最大在线人数”包含两个可能原因，不能直接判定为限流，也不能忽略后继续假报成功。详细对照见 [CAPTURE_ANALYSIS.md](CAPTURE_ANALYSIS.md)。

文件修改后需退出正在运行的旧进程再启动。显示请求方法、路径、参数名、响应状态和传输异常：

```powershell
py -3 .\course_helper.py --trace-http
```

诊断使用系统代理，日志不输出密码、验证码或 Cookie 值。`HTTP ->` 表示准备发起请求，`HTTP <-` 表示收到响应；只有前者而随后报传输异常，不能说明服务端已收到请求。

Windows 上推荐 `py -3` 明确选择注册的 Python 3。已将两个入口脚本的 shebang 从 `env python3` 改为 `python3`，避免 `py 脚本.py` 因 PATH 命中 MSYS2 而与测试、OCR 所在环境不一致。

本地原始抓包回放测试：

```powershell
py -m unittest discover -s tests -v
```

## 二次开发指南

想要扩展功能、接入第三方通知、适配新模块或修改协议？请阅读详细的开发文档：
👉 **[二次开发指南 (DEVELOPMENT.md)](DEVELOPMENT.md)**

该文档涵盖：
- 项目分层架构与核心类设计；
- 认证与动态选课令牌流（`randomString` 导航链）；
- 离线抓包契约测试体系；
- 新增查询模块、接入消息推送、自定义验证码 OCR 的代码示例；
- 抓包逆向与敏感信息脱敏规范。
