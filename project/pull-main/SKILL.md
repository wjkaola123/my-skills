---
name: pull-main
description: 把工作区三仓(agent/platform/decision)的 main 分支更新到 origin 最新——主检出在 main 就 pull --ff-only,在其他分支就 fetch origin main:main 只快进本地 main,不动工作区、不 force、不碰开发分支。当用户说「拉代码 / 拉一下 main / 更新 main / 把三仓拉到最新 / 拉最新 / 同步远端」,或新任务开工前要把仓库刷到最新时使用本技能。
---

# 三仓 main 拉到最新

技能位于 `/home/wj/project/.zcode/skills/pull-main`,操作对象是
`/home/wj/project` 下 agent / platform / decision 三个独立仓的 main 分支
(拉取集以脚本内 `REPOS` 数组为准)。核心逻辑在脚本里,内部全绝对路径,任意 cwd 都能跑:

```bash
/home/wj/project/.zcode/skills/pull-main/scripts/pull-main.sh [--dry-run]
```

脚本保证(无人值守也安全):

- 先 `fetch origin --prune`;主检出在 main → `pull --ff-only origin main`;
  在其他分支或 detached → `fetch origin main:main` 只快进本地 main ref,工作区一个字节不动
- 一律不 force:本地 main 与远端分叉、或本地改动会被覆盖 → git 自己拒绝,脚本只报告
- main 被某个 worktree 检出而无法快进 → 报告该 worktree 路径,让人去那里执行 pull
- decision 仓 2026-10 起搁置,仍在拉取集里(远端基本不动,fetch 无害)
- 单仓失败不中断其余仓,结尾出汇总表

## 流程

1. 直接跑脚本(想先看会动什么就加 `--dry-run`)
2. 完成标准:三仓各有一条 UPDATE / OK / FAIL / SKIP 结论,FAIL 的都带原因
3. 有 `[FAIL]` 的仓把原因摆给用户定夺——分叉的可 rebase 也可 `reset --hard origin/main`,
   用户点头再动;main 被别的 worktree 检出的,提示去那个 worktree 执行 `git pull --ff-only`
4. 汇报:每仓 main 的 before → after(或已最新)、拉下的前几条提交、
   当前检出分支落后新 main 多少

边界:只更新 main。不 rebase/merge 开发分支(对齐走 `/worktree-merge`),
不跑 `npm ci` / build(拉完要干活时再说)。
