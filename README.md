# 哈尔滨理工大学选课辅助工具

> **严禁用于真实选课，功能有效性尚未验证!!!**

基于教务系统抓包协议开发，支持登录、课程查询、容量监测、验证码识别和选课提交。

### 安装依赖

```powershell
pip install -r requirements.txt
```

## 配置账号

凭据不会写入代码，可任选一种方式配置：

1. 命令行：`--username`、`--password`
2. 将 `config.example.json` 复制为 `config.json`
3. 环境变量：`JWZX_USERNAME`、`JWZX_PASSWORD`
4. 运行时交互输入

## 教务信息查询

```powershell
py -3 .\jwzx_portal.py
```

支持查询课表、成绩、考试安排、教学计划、校历、课程目录和学籍信息，结果保存在 `exports`。

监测选课入口：

```powershell
py -3 .\jwzx_portal.py watch --interval 30
```

## 选课辅助

课程查询结果中的 `epid` 是教学班唯一标识。也可按课程名、教师和班别筛选，但抢课时筛选结果必须唯一。

验证码默认由 `ddddocr` 自动识别。连续失败后会保存至：

- `state/captcha.jpg`
- `state/elective_captcha.jpg`

程序不会输出密码、验证码或 Cookie。`grab` 默认每 5 秒检查一次余量，只串行提交；接口返回成功仅代表提交响应成功，未独立回查已选列表。

## 调试与测试

查看 HTTP 请求诊断：

```powershell
py -3 .\course_helper.py --trace-http
```

运行离线测试：

```powershell
py -3 -m unittest discover -s tests -v
```

修改代码后，应先退出旧进程再重新启动

协议细节见 [CAPTURE_ANALYSIS.md](CAPTURE_ANALYSIS.md)，扩展开发见 [DEVELOPMENT.md](DEVELOPMENT.md)。