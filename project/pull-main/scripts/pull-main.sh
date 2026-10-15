#!/usr/bin/env bash
# pull-main.sh — 把工作区三仓(agent/platform/decision)的 main 更新到 origin/main 最新
#
# 安全保证(无人值守也安全):
#   - 主检出在 main            → git pull --ff-only origin main(分叉/本地改动会被 git 拒绝,不 force)
#   - 主检出在其他分支/detached → git fetch origin main:main 只快进本地 main ref,工作区一个字节不动
#   - main 被别的 worktree 检出导致无法快进 → 报告该 worktree 路径,让人去那里处理
#   - 单仓失败不中断其余仓,结尾出汇总表
set -uo pipefail

ROOT="/home/wj/project"
REPOS=(agent platform decision)
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

SUMMARY=()

# 找出 main 被检出在哪个 worktree(没被任何 worktree 检出则输出空)
main_worktree() {
  git -C "$1" worktree list --porcelain 2>/dev/null |
    awk '/^worktree /{wt=$2} /^branch refs\/heads\/main$/{print wt; exit}'
}

for repo in "${REPOS[@]}"; do
  dir="$ROOT/$repo"
  echo "===== $repo ====="

  if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "[SKIP] $dir 不是 git 仓库"
    SUMMARY+=("$repo|SKIP|不是 git 仓库")
    continue
  fi

  if ! git -C "$dir" fetch origin --prune --quiet 2>/dev/null; then
    echo "[FAIL] fetch origin 失败(网络/远端?):"
    git -C "$dir" fetch origin --prune 2>&1 | sed 's/^/  /'
    SUMMARY+=("$repo|FAIL|fetch 失败")
    continue
  fi

  cur=$(git -C "$dir" branch --show-current)
  before=$(git -C "$dir" rev-parse --verify -q --short main)
  behind=$(git -C "$dir" rev-list --count main..origin/main 2>/dev/null || echo "?")
  ahead=$(git -C "$dir" rev-list --count origin/main..main 2>/dev/null || echo "?")

  if [ "$DRY" = 1 ]; then
    if [ "$behind" = "?" ]; then
      echo "[DRY] main 或 origin/main 缺失,需人工确认"
    elif [ "$ahead" != "0" ] && [ "$ahead" != "?" ]; then
      echo "[DRY] 本地 main 领先 origin/main $ahead 个提交(分叉;实跑只会 FAIL 报告,不会 force)"
    elif [ "$behind" = "0" ]; then
      echo "[DRY] main 已是最新($before)"
    else
      target=$(git -C "$dir" rev-parse --short origin/main)
      if [ "$cur" = "main" ]; then
        how="主检出在 main,将走 pull --ff-only origin main"
      else
        how="主检出在 ${cur:-detached},将走 fetch origin main:main(不动工作区)"
      fi
      echo "[DRY] main 可快进 $behind 个提交:$before → $target;$how"
    fi
    SUMMARY+=("$repo|DRY|见上")
    continue
  fi

  if [ "$cur" = "main" ]; then
    if ! out=$(git -C "$dir" pull --ff-only origin main 2>&1); then
      echo "[FAIL] pull --ff-only 被拒(分叉或本地改动会丢,不 force,交人工定夺):"
      printf '%s\n' "$out" | sed 's/^/  /'
      SUMMARY+=("$repo|FAIL|main 拉取被拒(分叉或本地改动)")
      continue
    fi
  else
    if ! out=$(git -C "$dir" fetch origin main:main 2>&1); then
      echo "[FAIL] 快进本地 main 失败:"
      printf '%s\n' "$out" | sed 's/^/  /'
      wt=$(main_worktree "$dir")
      if [ -n "$wt" ]; then
        echo "  main 被检出在 $wt——去那个 worktree 里执行 git pull --ff-only"
        SUMMARY+=("$repo|FAIL|main 被 worktree 检出($wt)")
      else
        SUMMARY+=("$repo|FAIL|main 快进被拒(疑似分叉)")
      fi
      continue
    fi
  fi

  after=$(git -C "$dir" rev-parse --verify -q --short main)
  if [ -z "$before" ]; then
    echo "[UPDATE] 本地 main 新建于 $after"
    SUMMARY+=("$repo|UPDATE|新建 $after")
  elif [ "$before" = "$after" ]; then
    echo "[OK] main 已是最新($after)"
    SUMMARY+=("$repo|OK|已最新($after)")
  else
    n=$(git -C "$dir" rev-list --count "$before..main" 2>/dev/null || echo "?")
    echo "[UPDATE] main $before → $after(快进 $n 个提交)"
    git -C "$dir" log --oneline "$before..main" 2>/dev/null | head -5 | sed 's/^/  /'
    SUMMARY+=("$repo|UPDATE|$before → $after(+$n)")
  fi

  if [ -n "$cur" ] && [ "$cur" != "main" ]; then
    lag=$(git -C "$dir" rev-list --count "$cur..main" 2>/dev/null || echo "?")
    echo "[INFO] 主检出在 $cur(未动),落后新 main $lag 个提交;对齐走 /worktree-merge"
  elif [ -z "$cur" ]; then
    echo "[INFO] 主检出处于 detached HEAD(未动)"
  fi
done

echo
echo "----- 汇总 -----"
for line in "${SUMMARY[@]}"; do
  IFS='|' read -r repo tag detail <<< "$line"
  printf '%-9s %-7s %s\n' "$repo" "$tag" "$detail"
done
