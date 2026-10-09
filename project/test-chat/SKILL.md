---
name: test-chat
description: 跑一遍机器人语音链路全流程测试（Playwright 打开 web 面板 → 登录 → 机器人链路 → 固定设备「Playwright 自动化设备」重置 token → 链路测试页 WS 连接 → 发文本触发 LLM+TTS），期间帧级监控浏览器 WS 事件流 + gateway 日志，产出双侧重叠时间线供深入讨论调用链。当用户说"跑一下链路测试 / 测机器人链路 / 发句话给机器人试试 / 验证 LLM+TTS 全链路 / test-chat"，或想看一句话从 WS 进去到语音出来中间发生了什么时，使用本技能。
---

# test-chat：机器人语音链路全流程测试

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/test-chat`），操作对象在 `/home/wj/project/agent` 仓。

一句话进、语音出，中间经过 web 面板 → gateway `/xiaozhi/v1/` → supervisor → LLM/TTS。
本技能把整条链路跑通，并在**两侧同时取证**（浏览器 WS 帧流 + gateway 日志），
让"深入讨论执行流程"有数据可依，而不是凭印象猜。

## 为什么这样设计

- **浏览器进程独占**：脚本用 `launchPersistentContext` 起 Chromium，整个回合（取设备 → 连接 → 发送 → 监控）
  由一个进程完成。这样不需要跨进程 attach 浏览器（本机 WFP 拦回环入站，CDP TCP 端口连不通，实测踩坑）。
- **固定设备 + 每回合重置 token**：不每次新建设备（避免库里堆积 test-chat 设备），而是复用
  「Playwright 自动化设备」，每回合 `POST /robots/:id/reset-token` 换新 token（旧的立即作废）。
  请求在登录后的页面上下文里发（`/gateway-api` 代理 + `localStorage.dbca_token` Bearer），无需单独鉴权。
- **用户文本走文件交接**：脚本连上后停在 `WAITING_FOR_PROMPT`，轮询 `/tmp/dbca-test-flow/prompt.txt`。
  你问完用户、把答案写进这个文件，脚本就会发送。这让"询问用户"和"浏览器自动化"解耦。
- **帧级而非 UI 级取证**：通过 `page.on('websocket')` 抓每帧的完整 JSON（`ws-trace.jsonl`），
  比 scrape 页面上的"事件流水"更全（二进制帧只记字节数）。UI 上的流水仍可肉眼对照。
- **gateway 侧走 tee 日志，不动进程**：gateway 可能是用户自己终端跑着，也可能是 startup 技能
  拉起的后台任务，杀掉重起都会打断使用者。`.env` 已开 `GATEWAY_LOCAL_LOG_ENABLED=1`，进程把 stdout/stderr tee 到
  `packages/gateway/logs/gateway/gateway.log`（`local-log-tee.ts`，50MB×5 轮转），跨会话可读。
  回合窗口用「发话前记字节偏移 → 结束后读增量」切出，见步骤 2/4/6。
- **分析先定题**：回合结束不泛读日志。第 6 步先问用户"本次分析的核心内容是什么"，
  日志检索和汇报都以回答这个问题为主线，避免每次都输出大而全的全链路 walkthrough。
- **回合终止看 `turn_metric`**：gateway 在回合真正结束时下发 `turn_metric` 帧（含
  `phases.llmTtftMs / llmTotalMs / replyCompleteMs`、`reply_chars`）。**`tts stop` 不可靠**——
  TTS 失败（如缺 `DASHSCOPE_API_KEY` 的 401）时 stop 会提前来，而 LLM/tool 还在跑、答案照常流出。
  脚本用「turn_metric 后静默 2s」判终，兜底「tts stop 后静默 5s」。

## 前置事实（勿重新推导）

| 项 | 值 |
|----|----|
| 脚本 | `node /home/wj/project/.zcode/skills/test-chat/scripts/run.cjs`（任意目录可执行；playwright 从 agent 仓 node_modules 解析）|
| 产物目录 | `/tmp/dbca-test-flow/`：`device.json`（固定设备记录 + 最新 token，仅供排查）、`prompt.txt`（待发文本）、`ws-trace.jsonl`（帧追踪）、`profile/`（登录态）|
| web 地址 | `http://localhost:5173`（可用 `TEST_FLOW_WEB_URL` 覆盖；账号 `TEST_FLOW_USER/PASS`，默认 admin/admin123）|
| 回合上限 | 默认 180s，`--timeout N` 覆盖；固定设备记录坏了（删不掉/连不上）加 `--new-device` 删除重建 |
| gateway 日志 | `/home/wj/project/agent/packages/gateway/logs/gateway/gateway.log`（stdout/stderr tee；`.env` 已开 `GATEWAY_LOCAL_LOG_ENABLED=1`，50MB×5 轮转）|

**已知环境问题**（见 startup skill "已知无害告警"）：本机 `.env` 缺 `DASHSCOPE_API_KEY` 时
TTS 上游 401：监控里会看到 `alert Unexpected server response: 401`、`tts stop`、二进制下行 0 帧。
这是配置缺失不是链路 bug——LLM 文本事件照常流动，照样值得讨论；汇报时注明即可。

## 步骤

### 1. 预检

- 三个服务必须在跑（`ss -ltn` 查 3000/3001/5173）。没跑 → 先执行 startup skill。
- 上一次 test-chat 的浏览器可能还开着并锁着 profile（脚本报 `ERROR_PROFILE_LOCKED`）。
  先 `/tasks` 找旧任务 TaskStop，或让用户关掉那个浏览器窗口。

### 2. 定位 gateway 日志（tee 文件，不动进程）

链路另一侧的证据来自本地 tee 日志，**不杀、不重启 gateway**——它由用户自己的终端或
startup 技能的后台任务跑着。

- 文件：`/home/wj/project/agent/packages/gateway/logs/gateway/gateway.log`（`.env` 已设 `GATEWAY_LOCAL_LOG_ENABLED=1` +
  `GATEWAY_LOG_DIR=./logs/gateway`，相对 gateway 进程 cwd；实现见
  [local-log-tee.ts](/home/wj/project/agent/packages/gateway/src/local-log-tee.ts)。仓库根的 `logs/gateway/`
  是历史残留空目录，别用）。
- 验证活性：`tail -1` + 看 mtime。finance scheduler 心跳几乎每分钟写日志，文件超过 ~3 分钟没动
  = gateway 是在开 tee 之前起的老进程（或 tee 被关了）。让用户**在自己终端**重启 gateway，
  本技能不代杀；新进程起来后日志头会有 `[gateway-log] local log tee enabled file=...` 确认行。

### 3. 启动脚本（后台）

```bash
node /home/wj/project/.zcode/skills/test-chat/scripts/run.cjs
```

轮询任务输出，依次等这些里程碑：`LOGIN_OK/LOGIN_SKIP` → `DEVICE_RESET_TOKEN`（或首次
`DEVICE_CREATED`）→ `SESSION_ESTABLISHED` → `WAITING_FOR_PROMPT`。

- 每回合对固定设备「Playwright 自动化设备」重置 token（日志 `DEVICE_RESET_TOKEN`，旧 token 立即作废）；
  库里没有该设备时首次走 UI 新建（日志 `DEVICE_CREATED`），之后再跑就都走重置。
- 卡在 `SESSION_ESTABLISHED` 之前：按脚本错误提示处理（gateway 死 → startup skill；设备状态异常 → `--new-device`）。

### 4. 拿用户要发的文本

- **用户本轮已经说了要发什么** → 直接用，不要再问。
- 否则用 AskUserQuestion 问"要发给机器人的文本是什么？"，给两三个预设
  （如"给我讲一个小兔子的故事"/"你会做什么？"）+ Other 自由输入。

然后把文本写入 `/tmp/dbca-test-flow/prompt.txt`（Write 工具，UTF-8，先确认旧文件已不存在——
脚本读完即删，残留旧文件会导致发错内容）。脚本检测到文件后自动发送。

**写文件前先记录 gateway 日志偏移**（供第 6 步切窗）：

```bash
wc -c < /home/wj/project/agent/packages/gateway/logs/gateway/gateway.log
```

把字节数记进对话（如 `OFFSET=2215890`）。

### 5. 监控回合

持续轮询脚本任务输出，把这些行实时转述给用户（这是"监控 LLM 输出"的部分）：
`llm`（含 emotion/agent/model）、`stt`、`tool`（含成败与耗时）、`tts sentence_start`（LLM 逐句输出）、
`alert`（异常）。收到 `=== SUMMARY ===` 即回合结束。

### 6. 对齐 gateway 侧证据

**先问重点，再读日志**：`SUMMARY` 后不要直接泛读日志窗口。先用 AskUserQuestion 问
"本次分析的核心内容是什么？"——预设几个常见视角（如"路由为什么选了这个板块/Agent"/
"LLM/tool 执行到哪一步、卡在哪"/"延迟都花在哪"）+ Other 自由输入。用户已在本轮对话里
说过想弄清什么就直接用，不要再问。之后的日志检索关键词、时间线挑帧、第 7 步汇报结构
都围绕这个重点展开。

然后从 tee 日志读增量（`OFFSET` 是第 4 步记录的字节偏移）：

```bash
tail -c +$((OFFSET+1)) /home/wj/project/agent/packages/gateway/logs/gateway/gateway.log
```

若此时文件大小 < OFFSET，说明回合中触发了 50MB 轮转 → 改读 `gateway.1.log` 尾部 + `gateway.log`
全量拼接。窗口内找：路由决策（板块/群组/Agent 选择）、supervisor/LLM 调用、tool 执行、
TTS 流水、错误栈。

**时间戳现实**：tee 日志行格式混合——pino JSON 行带 `time`（epoch ms，换算成 `HH:MM:SS.mmm` 本地
时间可用），console 行（`[RobotWsHub] …`、`{"type":"gate.thinking"…}` 等）**没有时间戳**。
所以主时间线以浏览器侧 `ws-trace.jsonl` 为准（每帧带 `ts`），gateway 行靠内容锚点
（session id、reqId、reply 文本）+ 出现顺序挂到时间线上，不要指望逐行对时。

### 7. 汇报（固定格式）

```
## 链路测试报告
- 核心结论（先回答第 6 步定下的分析重点，附支撑它的关键帧/日志行；用户没给重点才退回全链路概览）
- 回合：文本 / 耗时 / tts_stopped
- 时间线（两侧对齐，浏览器帧 ← / gateway 日志行，各挑关键 5~10 条）
- 异常与解释（如 TTS 401 → 缺 DASHSCOPE_API_KEY）
- 产物：ws-trace.jsonl / device.json / gateway.log@OFFSET 增量窗口
```

深挖时优先读 `ws-trace.jsonl`（每行一个 JSON：`{ts, dir, data}`，`data.type` 区分事件；
二进制帧是 `{bin: 字节数}`）。用户想讨论哪一环，就把对应帧/日志行捞出来展开。

## 边界

- 同一时间只能跑一个实例（profile 锁）。
- **不杀、不重启任何服务进程**：gateway 侧证据一律来自 tee 日志文件；需要重启 gateway
  （老进程没带 tee、或补 `DASHSCOPE_API_KEY`）时由用户做——自己终端起的自己重启，startup 技能
  后台任务则 TaskStop 后重跑 startup skill，本技能不代杀。
- 本技能不改 `.env`、不填任何密钥；TTS 401 的修复（补 `DASHSCOPE_API_KEY` + 重启 gateway）由用户决定。
- 回合结束浏览器保持开启（用户要看 UI 数字孪生/事件流水）；TaskStop 脚本任务或关窗口即收尾。
