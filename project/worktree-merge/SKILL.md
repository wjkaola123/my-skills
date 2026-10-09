---
name: worktree-merge
description: 把一个 git worktree 上的修改（含未提交改动）合并到指定分支——默认本地 merge --no-ff 进 main 并推送，覆盖 agent/ 与 platform/ 双仓。当用户说"把 ag-XX 合到 main / 合并 worktree / 把 worktree 的改动合到某分支 / 把这个分支合过去 / merge worktree / 分支收尾合并"时使用本技能。
---

# 把 worktree 修改合并到指定分支（双仓）

对象是 `/home/wj/project/agent` 与 `/home/wj/project/platform` 两个仓。
worktree 位置以 `git -C <repo> worktree list` 为准（不要枚举固定目录：
agent 的 worktree 同时存在于 `worktrees/` 与 `agent/worktrees/`，
platform 的在 `platform-worktrees/`），主检出在仓库根、通常停在 main。

先解析三个输入：

- **源**：worktree 路径 / 分支名 / AG 单号，三者任一即可定位；
- **目标分支**：默认 `main`；
- **方式**：默认本地 `merge --no-ff` 后推送。用户要 MR 时改走
  push option 建 MR（见第 5 节），两条路互斥，别双路走。

## 0. 定位源，处理双仓歧义

**同名分支两仓都有**（如 ag-86 在 agent 与 platform 都存在）：分别看
`git -C <repo> log origin/main..<branch> --oneline`，只有一边有实质提交就选
那边；两边都有 → 列出两边的提交摘要问用户合哪个（或都要）。

**detached HEAD 的 worktree**：没有分支没法合，先和用户确认基分支再
`git switch -c <branch>` 落成分支。

目标分支 == 源分支 → 直接报错停止。

## 1. 源 worktree 预检（顺序执行，任一步不过就停下报告）

1. **核对分支**：`git -C <wt> branch --show-current` 必须与预期一致。
   并行会话或 GitHub Desktop 可能把分支切走了（有改动消失的先例）。
2. **index.lock**：存在 `.git/index.lock` 说明 GitHub Desktop 在操作，
   轮询等释放（几秒一次，最多约 1 分钟），超时报告。
3. **未提交改动**，分三类处理：
   - 已跟踪文件的修改/删除 = 用户说的"修改"，先在源分支提交。
     提交信息用 conventional commit + AG 单号（从分支名提取），
     如 `fix(story): AG-87 resume 点名续听修复`；
   - 未跟踪文件：**排除** `node_modules`、软链（`git add -A` 会把软链
     node_modules 提进去，有先例）、一次性脚本（`.start-gw-*.sh`、
     `_t_*.mts` 之类）。其余逐个判断归属，拿不准问用户；
   - 无改动：跳过提交，直接走分支合并。
4. **展示将合并的内容**给用户过目：
   `git log <target>..<branch> --oneline` + `git diff <target>...<branch> --stat`。
   若目标已完全包含源（log 为空）→ 无事可合，报告并转第 5 节收尾。

## 2. 目标分支就位

先 `git -C <repo> fetch origin --prune`（MR 在 GitLab 上合，本地 main 常落后）。

远端分支消失（`origin/<branch>` gone）**不等于已合入**——MR 可能被关闭或分支
被误删（ag-87 有先例：远端删了、补丁不在 main）。用补丁级比对下结论：

```bash
git cherry origin/main <branch>   # 全 '-' = 补丁已在 main；有 '+' = 未进 main
```

全 `-`（典型：GitLab squash 合并后删分支）→ 不要再本地合，转 worktree-cleanup
收尾；有 `+` → 改动确实没进 main，正常走本地合并。

然后确定在哪儿执行 merge：

- **目标已检出**（主检出的 main，或挂在该分支上的 worktree）→ 就在那里执行。
  该检出必须无已跟踪文件的未提交改动，否则报告等用户处理；
- **目标没被任何地方检出** → 用临时 worktree，不动主检出
  （主检出跑着 dev 服务，切分支会干扰）：
  ```bash
  git -C <repo> worktree add /tmp/merge-<target> <target>
  # 在 /tmp/merge-<target> 里执行 merge，成功推送后：
  git -C <repo> worktree remove /tmp/merge-<target>
  ```

目标是 main 且在主检出时：`git pull --ff-only` 对齐 origin/main。
本地 main 领先 origin（有未推送合并）→ 提示先推；已分叉 → 报告，别自动解决。

## 3. 合并

```bash
git merge --no-ff <branch> -m "Merge branch '<branch>' into <target>（AG-XX 一句话描述，YYYY-MM-DD）"
```

消息格式是仓内既有习惯，日期用当天。`--no-ff` 保留 merge commit，
除非用户明说 squash / rebase。

**冲突** → 停下，调用 [resolving-merge-conflicts](/home/wj/.zcode/skills/resolving-merge-conflicts/SKILL.md)
技能解决，解决后继续。实在解不开（反复失败，或双方改动语义相悖、拿不准
该保哪边）→ 保留合并现场（**不要** `git merge --abort`），把冲突文件清单、
双方改动意图列给用户，请用户手动解决；用户处理完再继续第 4 节。

## 4. 验证与推送

合并完成后在目标检出 `git status` 确认干净，然后推送——用户要求合并即默认
包含推送，这是流程闭环：

```bash
git push origin <target>
```

- 凭据已存 `~/.git-credentials`，直接推；
- 推送失败（凭据失效）→ 用 Windows 侧 git 的临时 credential helper
  一次性推送（D 盘 Windows git 有 GCM 凭据），推完即还原 helper；
- 目标分支无远端 → 只本地合并，不推，报告即可。

**MR 变体**（用户明确要 MR 时，替代本地合并第 2–4 节）：推源分支
`git push origin <branch> -o merge_request.create -o merge_request.target=<target>`，
再按需加 remove/source 分支等 push option。

## 5. 收尾

- 源 worktree 已合入 → 提示可用 worktree-cleanup 技能清理（它以
  origin/main 为判定基准，等目标分支推送成功后再跑）；
- 合并动了 `package.json` / lockfile → 提醒主检出 `node_modules` 可能过期
  （tsc 报 TS2305/缺字段时先对 lockfile 查包版本，先 `npm install` 再归因）；
- 汇报：合并了哪些提交、目标分支的新 HEAD、是否已推送、worktree 去留建议。
