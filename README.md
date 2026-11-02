# cx-engine

> 配置：先复制 `config.example.json` 为 `config.json`，填写 ACCOUNT / COURSE_ID，
> 密码建议走环境变量（USE_ENV_PASSWORD=true + CX_PASSWORD）。
> 运行：`python -X utf8 cx_auto.py`（单门课）或 `python -X utf8 parallel_run.py --courses all`
> 视觉答题工具：`tools/quiz_repair.py`（截图→视觉模型作答→页面自己的提交按钮），
> `tools/oc_eyes_go.py` 为其模型客户端（key 从环境变量或 ~/.dsh/opencode_key.txt 读取）。

学习通（超星）课程自动化夹具。全流程在本机 Chrome 里跑。

> **状态**：见 `STATUS.md`（一页速览）｜ 待人工事项见 `CHECKLIST.md` ｜ 完整技术记录见 `HANDOFF.md`

---

## 先读这一段：几个反直觉的事实

这些是实测出来的，**和页面提示、和直觉都不一致**：

| 你以为 | 实际 |
|---|---|
| 视频看到 90% 就行（页面这么写的）| **必须完整看到结尾**。停在 90% 永远不记账 |
| 拖进度条能省时间 | **不算**。位置 ≠ 观看时长，拖过去不加观看时间 |
| 右上角倍速能开 2 倍 | **没用**。`playbackRates()` 返回 `null`，菜单里 0 个条目 |
| 一节课是一个任务点 | **每个标签页各算一个**。只做默认那个会一直差几个 |
| 提交按钮点了就交了 | **要看回执**。平台会因「还有 N 题未作答」拒收，而表单照样返回 |
| 填空题是输入框 | **是 iframe 里的富文本编辑器**，`input[type=text]` 匹配不到 |

---

## 怎么跑

```powershell
cd D:\Work\cx-engine

# 全自动（等待额度重置 + 活跃时段，然后自己开跑）
.\.venv\Scripts\python.exe -X utf8 schedule_resume.py

# 只跑一天的量（不等待）
.\.venv\Scripts\python.exe -X utf8 supervise.py

# 检查环境能不能开跑
.\.venv\Scripts\python.exe -X utf8 preflight.py

# 看还剩多少（只读）
.\.venv\Scripts\python.exe -X utf8 report_progress.py

# 自己扫码登录（凭据失效时）
.\.venv\Scripts\python.exe -X utf8 login.py --qr --interactive
```

---

## 执行链

```
schedule_resume.py   额度可用 + 07:00-23:00 才启动
      ↓
preflight.py         17 项检查；不过就不开机（登录失效会自动重登）
      ↓
supervise.py         轮次管理 + 孤儿 chrome 清扫
      ↓
parallel_run.py      按课程并发（默认 2）
      ↓
cx_auto.py worker    每个课程一个进程
```

**额度**：`logs/daily_budget.json` 限制每天 60 个任务点（`DAILY_TASK_CAP`）。**所有入口共享这个额度** —— 视频批次、测验扫描、单章工具都受约束。

---

## 文件

### 运行核心

| 文件 | 作用 |
|---|---|
| `cx_auto.py` | 浏览器驱动 + 答题 + 提交。`Session.do_quiz` 是**答题逻辑的唯一实现** |
| `task_points.py` | 视频/PDF/阅读页任务点处理 |
| `course_nav.py` | 章节导航与 URL 构造 |
| `node_cascade.py` | 读取侧边目录的节点与完成状态（`h5#cur<id>`）|
| `bank.py` | 答题引擎：缓存 → 题库文件 → sqlite → 远程接口 → AI |
| `ai_answers.py` | AI 答题层（读 `~/.dsh/.credentials.yaml` 或环境变量）|
| `humanize.py` | 防识别：抖动延时、指针游走、休息、额度、活跃时段 |
| `session_guard.py` | 登录态健康检查 |

### 入口

| 文件 | 作用 |
|---|---|
| `sweep_quizzes.py` | 全课程章节测验扫描（委托 `do_quiz`）|
| `auto_quiz.py` | 单章测验（委托 `do_quiz`）|
| `parallel_run.py` | 多课程并发调度 |
| `supervise.py` | 监督器：轮次 + chrome 清扫 + 每日测验扫描 |
| `schedule_resume.py` | 等额度重置 + 活跃时段，自动拉起 |
| `report_progress.py` | **只读**进度核对 |

### 验证（`preflight.py` 每天自动跑）

| 文件 | 覆盖 |
|---|---|
| `preflight.py` | 全链路接线 + 登录自愈（17 项）|
| `selftest.py` | 答案引擎端到端 |
| `test_logic.py` | 纯逻辑：答案规范化、填空拆分、判断映射（40 项）|
| `test_parking.py` | 防浪费：计数、停放、重启保持（10 项）|
| `test_scheduler.py` | 开工判断：额度 × 时段 × 边界（13 项）|
| `audit.py` | 静态审计：签名不匹配、静默 except、未定义变量、导入（6 类）|
| `audit_config_defaults.py` | 配置默认值被 `null` 击败的陷阱 |
| `verify_coverage.py` | 工作列表是否覆盖全部未完成节点（逐课程）|

### 数据

| 路径 | 内容 |
|---|---|
| `config.json` | 全部可调项 |
| `evidence/storage_state.json` | 登录态（`Session.start()` 读这个）|
| `evidence/course_cards.json` | 课程列表（courseId / clazzid / cpi）|
| `evidence/bank.json`、`evidence/tiku.db` | 学到的答案 |
| `logs/daily_budget.json` | 每日额度用量 |
| `logs/skip_nodes.json` | 反复不记账、已被停放的节点 |
| `logs/node_attempts.json` | 每个节点的尝试次数 |
| `logs/parallel/run_<courseId>.log` | 各课程 worker 日志 |

---

## 风控约束（代码里强制）

- 并发 ≤ 2，**同课程禁止并发**
- 章节间隔 75-210 秒随机
- 每日额度 60 点，**三个入口共享**
- 只在 07:00-23:00 工作（凌晨不间断比任何单个请求都显眼）
- 每 12 章休息 3-7 分钟
- 所有等待带抖动，不落在整秒
- 登录态失效自动重登，**900 秒冷却**（密码错时不会变成登录循环）

---

## 已知限制

1. **受保护视频只能 1x** —— 站点关闭倍速，拖拽不记账。25 分钟的片子就是 25 分钟。
2. **字体混淆的课程**（如思政）题干在 DOM 里是替换字符，本地题库复用率低，靠 AI 逐题作答。
3. **部分节点报成功但平台不记账** —— 用停放机制处理（同一节点试 3 次不成即跳过）。
4. **军事理论 100% 需要人工扫脸**，无技术手段可绕。

