# 在另一台电脑上部署 cx-engine

> 打包时间：2026-10-17 ｜ 内容：58 个文件，0.68 MB

---

## ⚠️ 先读这一条

**同一时间只能有一台机器在跑。**

平台限制**一个账号只能有一个活跃会话**。两台机器同时跑 = 两个会话 = **互相踢下线**，
表现为「视频播到一半页面跳回首页」，看起来像技术故障。

**换机器时，先在这台停掉**：

```powershell
# 在原机器上停掉计划任务
Stop-ScheduledTask -TaskName "cx-engine-daily"
Unregister-ScheduledTask -TaskName "cx-engine-daily" -Confirm:$false
# 并杀掉残留进程
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'cx-engine' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

（手机 App 也算一个会话 —— 别同时开着。）

---

## 需要什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10/11 | 用了 `msvcrt` 文件锁和计划任务 |
| Python | **3.11 或更高** | 需含 `sqlite3`（标准库自带）|
| 浏览器 | **Chrome 或 Edge** | 脚本用本机安装的浏览器；不用的话会退回 Playwright 自带的 chromium |
| 网络 | 能访问 chaoxing.com 和 api.deepseek.com | AI 答题走 DeepSeek |
| DeepSeek Key | `~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY`，或环境变量 | 没有则只能靠本地题库 |

---

## 一键安装

把整个 `cx-engine` 文件夹拷到新电脑（任意路径都行，**没有写死的绝对路径**），然后：

```powershell
cd <你放的位置>\cx-engine
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

脚本会依次做：

1. 检查 Python 版本
2. 建虚拟环境 `.venv`
3. 安装依赖（`playwright` + `requests`）
4. 检查 Chrome/Edge
5. 建 `logs\` 和 `evidence\` 目录
6. 跑一遍自检（preflight 的离线部分）
7. 提示你登录

---

## 手动步骤（如果不用脚本）

```powershell
cd <你放的位置>\cx-engine

# 1) 建虚拟环境
py -3 -m venv .venv

# 2) 装依赖
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) 建目录
New-Item -ItemType Directory -Force logs, logs\parallel, evidence | Out-Null

# 4) 自检（不碰平台）
.\.venv\Scripts\python.exe -X utf8 preflight.py --no-network

# 5) 登录（弹出二维码，用学习通 App 扫）
.\.venv\Scripts\python.exe -X utf8 login.py --qr --interactive

# 6) 完整自检（含登录态探测）
.\.venv\Scripts\python.exe -X utf8 preflight.py
```

第 5 步也可以不扫码：`config.json` 里已经带了账号密码，直接跑
`preflight.py`（不带 `--no-network`）会自动用凭据登录并保存会话。

---

## 注册每日自动运行

```powershell
$root = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$root\run_daily.cmd`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 07:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 18) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "cx-engine-daily" -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal -Force
```

**为什么用计划任务而不是普通进程**：2026-10-17 我把它作为会话后台任务启动，会话一挂起
它就被系统终止了（`DBG_TERMINATE_PROCESS`），**2.5 天无声无息地没了**，日志里什么都没有
（因为是被杀的，不是崩的）。计划任务由操作系统托管，不依赖任何登录会话。

**执行时间上限 18 小时**是为了兜底：正常情况下 23:00 时段守卫会让它自己收工。

---

## 目录说明

```
cx-engine/
├─ 运行核心（必需）
│   cx_auto.py            浏览器驱动：任务点处理 + 内联答题 + 验证式提交
│   task_points.py        视频/音频/PDF/阅读页处理
│   course_nav.py         章节导航与 enc 令牌刷新
│   node_cascade.py       读取侧边目录的节点与完成状态
│   bank.py               答题引擎：缓存 → 文件 → sqlite → HTTP → AI 兜底
│   bank_db.py / bank_server.py   本地题库
│   ai_answers.py         AI 答题层（DeepSeek）
│   humanize.py           防识别：抖动、休息、额度、活跃时段
│   session_guard.py      登录态健康检查
│   anomaly.py            异常标记检测（平台警告 → 当天停手）
│   safejson.py           加锁 + 原子写的 JSON 工具
│   login.py              扫码 / 凭据登录
│   supervise.py          监督器：轮次 + 清孤儿 chrome + 每日测验扫描
│   parallel_run.py       多课程调度（并发 1）
│   schedule_resume.py    四道闸门入口
│   make_worker_configs.py  生成每门课的 worker 配置
│   sweep_quizzes.py      章节测验扫描
│   auto_quiz.py          单章测验
│   report_progress.py    只读进度核对
│   tools.py              题库离线工具
│   run_daily.cmd         计划任务的入口（带日志重定向）
│
├─ 验证套件（preflight 会自动跑）
│   preflight.py          全链路 + 登录自愈 + 异常（20 项）
│   selftest.py           答案引擎端到端
│   test_logic.py         纯逻辑 40 项
│   test_parking.py       防浪费机制 17 项
│   test_scheduler.py     开工判断 16 项
│   test_anomaly.py       异常停止 10 项
│   test_budget_concurrency.py  额度并发 5 进程压测
│   audit.py              静态审计（签名/静默 except/未定义变量/导入）
│   audit_config_defaults.py    配置默认值被 null 击败的陷阱
│   verify_objective.py   目标 21 条逐条对上代码
│   verify_writes.py      写入点保护检查
│   verify_coverage.py    工作列表覆盖检查
│
├─ 诊断工具（只读，批次停止时用）
│   diag_video_state.py   逐秒观察视频状态
│   diag_frame_loss.py    记录媒体 frame 消失时的页面
│   probe_audio_controls.py  导出音频播放器控件
│   survey_remaining.py   抽样统计剩余节点类型
│   verify_media_once.py  单节点端到端验证
│   quick_status.py / check_progress.py / report_progress.py
│
├─ 文档
│   STATUS.md             **一页速览**（先看这个）
│   README.md             工程说明
│   CHECKLIST.md          待人工事项
│   HANDOFF.md            完整技术记录（160 KB 事件日志）
│
├─ config.json           账号、额度、间隔等全部可调项
├─ requirements.txt      依赖清单
└─ evidence/             登录态 + 学到的答案（已随包带一份种子题库）
```

---

## 常用命令

```powershell
cd <路径>\cx-engine

# 只读进度核对（会开浏览器 —— 只在批次停止时用）
.\.venv\Scripts\python.exe -X utf8 report_progress.py

# 看有没有异常标记
.\.venv\Scripts\python.exe -X utf8 anomaly.py

# 自检
.\.venv\Scripts\python.exe -X utf8 preflight.py

# 手动跑一天（不等待，直接开跑）
.\.venv\Scripts\python.exe -X utf8 schedule_resume.py

# 监督器的完整输出
Get-Content logs\scheduler.log -Tail 40
```

---

## 可调参数（`config.json`）

| 键 | 当前值 | 说明 |
|---|---|---|
| `DAILY_TASK_CAP` | 600 | 每日任务点上限（所有入口共享）|
| `SLEEP_BETWEEN_CHAPTERS` | `[60, 150]` | 章节间随机间隔（秒）|
| `SWEEP_MAX_MINUTES` | 25 | 每日测验扫描的时间上限 |
| `MAX_NODE_TRIES` | 3 | 同一节点失败几次后停放 |
| `HEADLESS` | true | 无头模式 |

**并发不在 config 里** —— 它是 `supervise.py` 里的 `CONCURRENCY`，当前是 `"1"`。
**不要调高**：平台限制一个账号一个活跃会话，3 和 2 都会被踢（实测）。

---

## 排障

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 视频播一半跳回首页 | 有第二个会话（另一台机器 / 手机 App / 你自己开了浏览器）| 关掉其它会话 |
| `Browser.new_context: Target crashed` | 孤儿 chrome 进程太多 | 已有三次重试；也可手动 `taskkill /F /IM chrome.exe` |
| 调度器不动 | 看 `logs\scheduler.log`；可能是 preflight 没过 | 手动跑 `preflight.py` 看哪项失败 |
| 登录失效 | 会话过期 | 自动重登（900 秒冷却）；不行就 `login.py --qr` |
| 测验分数低 | 题库没命中 | 看 `logs\unanswered.csv`，或检查 DeepSeek key |
| 夜里还在活动 | 不该发生（时段守卫已修）| 检查 `logs\scheduler.log` 的时间戳 |

---

## 最后

**先看 `STATUS.md`** —— 一页速览：剩多少、系统怎么跑、怎么自己查。
