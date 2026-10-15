---
name: skill-sync
description: 三端同步 Claude Code、ZCode、Codex CLI 的自定义技能（/home/wj/project/.claude/skills ↔ /home/wj/project/.zcode/skills ↔ ~/.codex/skills），任一侧新建/修改的技能同步到其余两侧。当用户说"同步技能 / skills 同步 / 把技能推到 zcode / 推到 codex / 把 zcode 的技能拉过来 / 检查技能有没有漂移"时使用。
---

# 技能三端同步（claude ↔ zcode ↔ codex）

## 三个技能目录

| 侧 | 路径 | 形态 |
|----|------|------|
| claude（Claude Code 项目级） | `/home/wj/project/.claude/skills/` | 自引用相对路径（`.claude/skills/<name>/...`），无部署说明头 |
| zcode（ZCode 工作区级） | `/home/wj/project/.zcode/skills/` | 自引用绝对路径 + 部署说明头（工作区视角） |
| codex（Codex CLI 用户级） | `~/.codex/skills/` | 绝对路径 + 部署说明头（注明用户级部署与触发语义） |

同步集以**工作区两侧**为准（两侧目录下的全部技能，均为用户自定义，无内置混入）；codex
侧是镜像目标。用户级目录（`~/.claude/skills/`、`~/.zcode/skills/`）**不在同步范围**。

Codex 侧规则（0.162 实测，2026-10-13）：
- Codex 无项目级技能目录，只能落用户级 `~/.codex/skills/<name>/SKILL.md`；frontmatter
  name/description 与 CC 同格式，直接可用。
- `~/.codex/skills/.system/` 是 Codex 内置系统技能，**永不碰**；用户手装的、工作区没有的
  技能（如 `unslop`）**不纳入同步集也不改动**。
- `codex` 技能本身（教 CC/ZCode 委托 codex exec）**不同步到 codex 侧**——在 codex 里自引用
  无意义；其余技能全量同步。
- Codex 里「技能触发」= 把 SKILL.md 读进上下文照做，无 CC 的 Skill 工具机制；技能内的
  文件引用都是普通路径，Codex 可直接读，包括指向 `.claude/skills/...` 的绝对路径。

2026-09-07 迁移注记：本机是 WSL2，双仓工作区根为 `/home/wj/project`（含 `agent/` 与
`platform/` 两仓）。两侧现在是工作区根下的**兄弟目录**，脚本定位逻辑统一为
「`scripts/ → <skill>/ → skills/ → .claude|.zcode/ → 工作区根`，4 级 `..` 后拼 `agent`」，
两侧一致，不再是旧的"仓内 4 级 vs 工作区 5 级"不对称。

## 核心规则

1. **双向传播，claude 侧优先**：
   - 一侧有的技能另一侧没有 → 拷贝过去 + 本地化改写（两个方向都要做）。
   - 两侧都有但内容不同 → 剥掉本地化差异比**实质内容**（步骤、命令、参数、表格行、脚本逻辑）：
     - 只有一侧有实质更新 → 以该侧为源更新另一侧。
     - 两侧都有实质更新（真冲突）→ **停下来向用户报告差异，让用户裁定**，不要自动合并。
2. **本地化差异不是漂移**，同步时必须保留/重建，不能抹掉：

| 差异点 | claude 版 | zcode 版 | codex 版 |
|--------|----------|----------|----------|
| 部署说明头 | 无 | SKILL.md 正文开头一段「本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/<name>`）……」 | 开头一段「部署在 `~/.codex/skills/<name>/`（用户级），由 /skill-sync 三端同步维护……」并注明触发语义差异 |
| 自引用路径 | `.claude/skills/<name>/...`（可相对） | `/home/wj/project/.zcode/skills/<name>/...`（绝对） | 绝对路径（`~/.codex/skills/<name>/...` 或原绝对路径不动） |

3. **机器适配是实质内容的一部分**：本机为 WSL2，Windows 时代的写法要按实际环境落地——
   `D:\Projects\agent` → `/home/wj/project/agent`；`D:\Tools\aip_client` → `/mnt/d/Tools/aip_client`
   （powershell.exe 互通）；`D:\story\audio` → `/home/wj/story-audio`（`.env` 的 `MEDIA_LIBRARY_DIR`）；
   `%TEMP%` → `/tmp`；`netstat -ano`/`taskkill` → `ss -ltnp`/`kill`（脚本内双平台分支最优先）；
   Docker 引擎为 WSL 原生 systemd 服务（`sudo systemctl start docker`），没有 Docker Desktop GUI。

## 操作流程

### ① 漂移检查（三端的缺失和差异一起看）

```bash
for root in /home/wj/project/.claude/skills /home/wj/project/.zcode/skills ~/.codex/skills; do
  for s in "$root"/*/; do
    name=$(basename "$s")
    for other in /home/wj/project/.claude/skills /home/wj/project/.zcode/skills ~/.codex/skills; do
      [ "$other" = "$root" ] && continue
      [ -d "$other/$name" ] || echo "+ $name 仅存在于 $root（目标 $other 缺失）"
    done
  done
done
# 排除项：~/.codex/skills/.system（内置）、codex 侧的 unslop（工作区没有）、codex 技能不进 codex 侧
# 同名技能的差异逐个 diff -rq + diff 看内容（diff 前先剥上表两类本地化差异）
```

### ② 逐技能裁定

对每个同名但内容不同的技能 `diff` 逐文件看：

- 差异**全部**落在上表两类本地化差异里 → 内容等价，跳过。
- 一侧多出实质内容 → 以该侧为底稿更新另一侧，再套目标侧的本地化（见 ③）。
- 两侧各多各的 → 列出两侧独有改动，问用户。

### ③ 拷贝 + 本地化改写

**claude → zcode**：
- SKILL.md：加部署说明头；`.claude/skills/<name>/...` 自引用 → `/home/wj/project/.zcode/skills/<name>/...` 绝对路径；仓内引用改绝对（`packages/...` → `/home/wj/project/agent/packages/...`）。
- scripts/：复制后把仓库定位改成 4 级 `..` + 拼 `agent`（两侧同构，能从 `__file__`/`__dirname` 推导的优先推导，勿硬编码）；npm 依赖（playwright/opusscript）显式指到 `/home/wj/project/agent/node_modules`，勿裸 require（工作区根没有 node_modules）。
- 跨技能引用同理改绝对（如 smoke_common 引 story-flow-test 的 encode_frames.js——用 `__file__` 推导则天然免改）。

**zcode → claude**：
- SKILL.md：去掉部署说明头；自引用绝对路径 → `.claude/skills/<name>/...`。
- scripts/：定位逻辑同构保留；Windows 分支（若有）勿删——脚本双平台化是加分项，回传时保留。

**→ codex（claude 或 zcode 定稿后镜像）**：
- SKILL.md：加 codex 部署说明头（用户级部署 + 三端同步维护 + 触发语义一句）；其余内容原样——
  技能里的路径引用全是绝对路径，Codex 直接可用，**不需要**把 `.claude/skills/...` 引用改写到
  `~/.codex/skills`（那份文件只是拷贝，权威源在工作区）。
- scripts/：按需带上（Codex 侧照跑的是同一台机器的文件系统）；`.system/` 与非同步集技能不动。
- `codex` 技能本身跳过（见同步集规则）。

### ④ 同步完汇报

每个技能一行：✓ 本来就一致 / → 方向已更新 / + 新增部署；列出做过的本地化改写点；有真冲突的单独列出用户裁定结果。

## 注意

- 只同步技能目录本身（SKILL.md + scripts/ + references/ 等），不带 evals/workspace 之类临时产物（`.cache/`、`__pycache__/`、`story_flow_result.json`、`_smoke_*_result.json` 等）。
- 本技能（skill-sync）自身也在同步集里：三端内容一致（谈的就是三个根路径，无本地化差异），直接镜像；codex 侧同样部署一份。
- 2026-10-15 全量同步已清账：zcode 侧 WSL 适配与双平台脚本(startup / stop-all / test-chat /
  story-flow-test 四技能的 SKILL.md + 脚本)已回传 claude 侧;codex 侧除 `codex` 技能本体与
  unslop 外已全量部署。claude↔zcode 残留差异仅为部署头与自引用路径两类本地化差异。
