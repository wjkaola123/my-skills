---
name: worktree-cleanup
description: 清理已合入 main 分支的 git worktree（agent/ 与 platform/ 双仓，worktrees/ 与 platform-worktrees/ 目录），删 worktree 及其已合入分支；也管理每日自动清理的 systemd 定时器。当用户说"清理 worktree / 删掉已合入的 worktree / worktree 太多了 / 清理分支工作区 / clean worktrees"，或问"定时清理装了吗 / 定时任务状态 / worktree 清理日志"时使用本技能。
---

# 清理已合入 main 的 worktree（双仓）

技能位于 `/home/wj/project/.zcode/skills/worktree-cleanup`，操作对象是
`/home/wj/project/agent` 与 `/home/wj/project/platform` 两个仓的 worktree
（分别在 `/home/wj/project/worktrees/` 与 `/home/wj/project/platform-worktrees/`）。

核心安全逻辑在脚本里，交互会话与每日定时器共用同一份：

```bash
/home/wj/project/.zcode/skills/worktree-cleanup/scripts/clean-merged-worktrees.sh [--dry-run]
```

脚本保证（无人值守也安全）：

- 判定基准是 **origin/main**（先 `fetch --prune`——MR 在 GitLab 上合，本地 main 常落后）；
- 只删「分支 tip 是 origin/main 祖先」的 worktree，主检出 / detached / locked 一律不碰；
- worktree 有未提交改动、或除 `node_modules` 外的未跟踪文件（如 `.env`）→ 跳过并报告；
- 孤儿目录（未注册到 git 的残留目录）**只报告不删除**；
- worktree 目录下的 `node_modules` 视为一次性产物，随 worktree 一起删；
- **本地与远端同步**：已合入的 `ag-*` 远程分支 `push --delete` 删除，已合入、
  未挂任何 worktree 的 `ag-*` 本地裸分支一并删；`feat/*`、`chore/*` 等非 `ag-*`
  分支即使已合入也不碰，push 失败仅告警下次再试。

## 交互清理流程

### 1. 先 dry-run

```bash
/home/wj/project/.zcode/skills/worktree-cleanup/scripts/clean-merged-worktrees.sh --dry-run
```

### 2. 审查输出，把需要用户决策的项摆出来

- `[SKIP]` 带"未提交/未跟踪改动"的：读一下改动内容，判断是一次性产物
  （如指向 worktree 自身的启动脚本）还是有用配置，拿不准就问用户；
- `[ORPHAN]` 孤儿目录：默认不删，列给用户确认（曾有 `.env` 先例）；
- `[KEEP]` 是未合入分支，无需处理。

### 3. 实际执行（去掉 --dry-run 重跑）

### 4. 汇报

列出：删了哪些 worktree + 分支（本地与远程）、跳过了哪些及原因、保留了哪些（未合入）。
远程 `ag-*` 分支与本地裸分支由脚本自动同步删除，无需手动处理；
非 `ag-*` 的已合入远程分支（如 `chore/*`）脚本不动，若用户想清需单独确认。

## 定时器管理

每日 13:00 由 systemd user timer 自动跑（`Persistent=true`，WSL 关机错过则下次补跑）。

```bash
# 安装 / 重装（幂等）：install-timer.sh [--schedule "<OnCalendar 语法>"]
/home/wj/project/.zcode/skills/worktree-cleanup/scripts/install-timer.sh

# 状态与下次触发时间
systemctl --user list-timers worktree-cleanup.timer

# 看定时清理日志
tail -50 /home/wj/project/.scratch/worktree-cleanup.log

# 手动触发一次定时任务（与交互执行等价，走日志文件）
systemctl --user start worktree-cleanup.service

# 停用 / 彻底移除
systemctl --user disable --now worktree-cleanup.timer
rm ~/.config/systemd/user/worktree-cleanup.{service,timer} && systemctl --user daemon-reload
```

注意：WSL 的 user timer 只在 WSL 实例运行时触发；`install-timer.sh` 会尝试
`loginctl enable-linger` 让 WSL 一启动 user manager 就绪，失败也不影响有会话时的触发。
