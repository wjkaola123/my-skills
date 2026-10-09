---
name: codex
description: 在 Claude Code 里把任务委托给本机 Codex CLI（OpenAI codex-cli，ChatGPT 账号已登录）非交互执行：单发任务（codex exec）、同会话追问（exec resume）、独立代码评审（exec review）。当用户说"用 codex 跑 / 丢给 codex / 让 codex 看看 / codex 第二意见 / 交叉评审 / 双模型对拍"或需要第二个外部 agent 并行干活时使用本技能。
---

# 调用 Codex CLI（非交互委托）

目标：把用户描述的任务组织成提示词，用 `codex exec` 跑通并取回结果。不是"命令发出去了"就算成功——必须拿到 codex 的 final answer 并如实转述（包括它失败/拒答的情况）。

## 固定事实（codex-cli 0.162.0 实测，勿重新推导）

- codex 在 PATH：`~/.nvm/versions/node/v24.20.0/bin/codex`；已用 ChatGPT 账号登录（`codex login status` 验证，输出 `Logged in using ChatGPT`）。
- 本版本**没有** `codex mcp-server` 子命令（已被实验性 `codex app-server` 自有协议取代，那不是 MCP）——**不能**注册成 Claude Code 的 MCP server，唯一通道就是 `codex exec`。
- `codex mcp` 子命令是管理 codex 自己用的外部 MCP server，与"被 Claude Code 调用"无关，别走错门。
- 工作区根 `/home/wj/project` **不是 git 仓库**（agent/、platform/ 才是）→ 调用必须带 `--skip-git-repo-check`，并用 `-C` 把工作根指到 agent/ 或 platform/（或具体包目录）。
- codex 是真实 LLM，消耗 ChatGPT 账号额度；单次几十秒到几分钟。Bash 调用给足 timeout（≥180s）或直接 `run_in_background`。
- `codex exec` 默认 approval=never，不会卡在等确认；能做什么由 `-s` 沙箱档位决定。

## 标准调用形状

单发任务（最常用）：

```bash
codex exec --skip-git-repo-check -C /home/wj/project/agent -s read-only -o /tmp/codex-last.md "<提示词>"
```

- `-s read-only`：只读，默认选它（安全）；要它改代码用 `-s workspace-write`（写范围限工作根 + `--add-dir` 追加）。
- `-o <file>`：把最后一条 agent 消息落盘——**取结果优先用这个**，stdout 是带日志的全文，靠 tail 截断易碎。
- `--json`：JSONL 事件流（适合 run_in_background 时流式观察进度）。
- `-m <model>`：覆盖模型；不指定用用户 config 默认。

同会话追问（需在同一 cwd，沿用最近线程）：

```bash
codex exec resume --last "<追问>"
# 或指定线程：codex exec resume <session-id> "<追问>"
```

独立代码评审（第二意见，须在 git 仓库内）：

```bash
codex exec review            # 评审当前仓库未提交/指定 diff
```

大提示词走 stdin（避免 shell 转义地狱）：

```bash
codex exec --skip-git-repo-check -C <dir> -s read-only - < /tmp/prompt.md
```

## 易错点

- 忘 `--skip-git-repo-check` 且 cwd 非 git 仓 → 直接拒跑；忘 `-C` → codex 在错误目录里瞎转。
- **别用** `--dangerously-bypass-approvals-and-sandbox`，除非用户逐字明确要求。
- WSL 下 `could not find bubblewrap` 警告可忽略（自动用 bundled 版）。
- stdout 末尾 `tokens used` 段之后还会重复一遍 final answer；解析 stdout 易出错，用 `-o` 落盘文件为准。
- 长任务放 `run_in_background`，完成后读 `-o` 文件；不要在前台干等 5 分钟。
- codex 的改动若要落进 git，走正常 review/commit 流程——它写盘后工作树状态要自己核实（`git -C <repo> status`），不要假设它说改了就是改了。

## 与 Claude Code 分工建议

- 适合丢给 codex：独立评审/第二意见、大范围只读调研、与 Claude 并行的另一条实现路线。
- 不适合：琐碎 grep/读文件（本机工具更快更省）、需要本工作区上下文记忆的活（codex 看不到本会话历史，上下文要写进提示词或让它自己读文件）。
