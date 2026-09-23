---
name: stop-all
description: 停止 agent 仓库的三个开发服务（gateway:3000 / admin-api:3001 / web:5173）并验证端口全部释放。当用户要求"停掉服务 / 停止所有服务 / 关掉 gateway、admin-api、web / stop the dev servers / 重启前先停干净"时使用本技能；EADDRINUSE 端口被孤儿进程占用时也用它清理。
---

# 停止 agent 三件套（gateway / admin-api / web）

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/stop-all`），操作对象在 `/home/wj/project/agent` 仓。

目标：三个服务进程全部退出、端口全部释放，并用 `ss` 证明。只杀外层
npm 不算停 —— 会留孤儿 node 继续占端口，下次启动必 EADDRINUSE（本机实测
踩过：tsx/nest 都是多级子进程）。

## 步骤

### 1. 若服务是本会话的后台任务，先 TaskStop

服务如果是自己（或上一个会话）用后台任务拉起的，先逐个 TaskStop 掉
gateway / admin-api / web 的任务 ID。这只是杀掉外层包装进程，**孤儿清理
必须继续走第 2 步**，不能省。

服务如果是用户自己在终端里起的（没有对应后台任务），直接走第 2 步。

### 2. 执行停止脚本

```bash
node /home/wj/project/.zcode/skills/stop-all/scripts/stop.mjs
```

脚本做的事：按 `.env` 解析出三个端口（与 verify.mjs 同一套规则：
`PORT`→gateway、`ADMIN_API_PORT`→admin-api 缺省 3001、web 固定 5173）→
`ss -ltnp` 找出每个端口的 LISTENING 进程 → SIGKILL 杀掉 →
复查端口全部释放。（脚本双平台：Windows 侧自动走 netstat + taskkill 分支。）

- 幂等：没有进程在听就直接报"无需停止"，退出码 0。
- 退出码 0 = 三端口全空；1 = 仍有端口被占（脚本会列出是哪个）。
- `ss -ltnp` 的 `pid=` 信息需要进程属主或 root 权限才可见——脚本拿不到
  pid 时会残留，用 `sudo` 重跑一次。

### 3. 汇报

列出每个服务：端口、杀掉的 PID（或"本就没在跑"）、端口是否已释放。
如有残留占用，给出 `ss -ltnp | grep :<port>` 的输出
让用户决策（可能是别的程序撞端口，别盲杀）。

## 关联

- 要重启：停完后直接用 startup 技能（`/home/wj/project/.zcode/skills/startup/`）拉起并验证。
- startup 技能的预检里"端口被占但验证不绿"场景，清理手段就是本脚本的逻辑。
