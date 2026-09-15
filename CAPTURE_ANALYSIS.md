# 2026-09-15 抓包重新核对

## 能确认的差异

| 项目 | 修改前 | 抓包与修正 |
| --- | --- | --- |
| Python 启动环境 | `env python3` 让 `py course_helper.py` 命中 PATH 中的 MSYS2，而 `py -m unittest` 使用注册的 Windows Python 3.14 | 实际诊断输出已证实两个不同路径；入口改为 `#! python3`，推荐显式 `py -3` |
| 选课入口导航 | 直接请求 elective.do，以自身作为 Referer | 旧完整包 102/103/117/119/121：框架、动态菜单、accessModule、jumppage、选课页。使用实时解析的 randomString，不复制旧令牌 |
| 页面 Referer | elective.do? | 更新包 8：来自 jumppage.jsp；接口调用才使用 elective.do? |
| 页面初始化 | 没有 electiveMgs.do | 更新包 11：选课信息初始化，位于状态请求前 |
| 验证码校验 | POST 缺少 Origin | 更新包 35：Origin 为站点，captchaCode 在 URL 中，Content-Length 为 0 |
| Accept | 静态课程 JSON、容量接口、图片与浏览器不完全一致 | 对照更新包 17/20/24 的各自 Accept；保留 Content-Type、X-Requested-With |
| 压缩 | gzip, identity，仅识别大写键的 gzip | 抓包 gzip, deflate；支持 gzip 与两种 deflate 流，头字段名大小写无关 |
| 开放判断 | 任意非登录 HTTP 200 页面均算开放 | 完成导航后由 electiveStatus.do 的状态确认 |
| 错误解释 | 将混合提示直接解释为人数限流，并曾忽略错误 | 明确标注选课会话未确认，保留端点和服务端原文，停止当前操作 |

这些是代码与成功抓包之间的已证实差异。尚不能仅靠离线材料证明其中哪一项是服务器拒绝该会话的唯一原因。

## 请求和参数事实

- 更新包 19 **存在** `findSelectedCidList.do?prop=0&_=...`。网页在显示待选列表时调用它，不应根据 cid 判定某个具体 epid 已成功选中。
- 完整包 184 **存在** `findElectiveFinshCourse.do`；更新包页面脚本明确由“已选课程”操作触发。当前脚本仍只在显式查看已选课程时查询它。
- 更新包 35 的验证码为 6 位，旧包 170 为 4 位；代码保留字符串，不截断、不补零、不固定长度。
- 更新包 36 采用 GET，参数只有 `epid` 和防缓存的 `_`；验证码保存在同一会话中，并不再作为提交参数传入。
- 容量 POST 使用重复的 `epid=...&epid=...` 字段，不使用 `epid[]` 或 JSON。
- 更新包选课页面声明 `selectCourseTime = "5.0"`，页面脚本在倒计时结束后退出；Cookie 存在不等于选课会话仍有效。
- 更新包 36 的成功响应包含外层 `status=0` 和内层 `result.status=0`、对应的 `epid`。没有独立回查时 `verified` 保持 false。

## 验证边界

`tests/test_capture_contract.py` 用原始抓包响应驱动本机 HTTP 服务器，逐个比对收到的方法、路径、非防缓存参数、请求头和 POST 正文。回放使用虚拟 Cookie，不向学校提交课程。

已选列表报超时/最大在线人数、提交缺少 result、提交 epid 不匹配等情况有单独验证；不会被当成成功。

本轮使用现有会话做的只读实网对照，在系统代理和直连两条路径均遇到连接被关闭，未取得可比较的 HTTP 响应，因此实网恢复尚未证实。

用户进一步确认：“抓不到请求”指 Python 运行时在 Reqable 中未显示，不是浏览器未调用这些接口。只读检查确认系统代理为 `127.0.0.1:9000`，该端口由 Reqable 监听，目标域名没有绕过代理。Python 的 socket 连接事件也确认实际连接了这个端口，随后抛出 `RemoteDisconnected`。这证明代理路径存在，但不能据此证明请求已转发到学校或归因于 Reqable 的某一项过滤设置；当前捕获界面的实时过滤/暂停状态未确认。

修正入口后再次运行 `py course_helper.py --trace-http selected`，已确认启动的是注册的 Windows Python 3.14。该次实网执行停在第一个 `GET /academic/frameset_index.jsp`，5 秒后 TimeoutError，后续查询尚未执行。两个 Python 版本都保留 URL 末尾的 `?`，因此不能将本次问题归因于 urljoin 丢失问号。
