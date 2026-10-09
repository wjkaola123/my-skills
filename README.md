# my-skills

个人 Claude Code / ZCode 技能全量镜像。最近同步：**2026-10-09**。

| 目录 | 来源 | 说明 |
|---|---|---|
| `project/` | `/home/wj/project/.zcode/skills` + `.claude/skills/codex` | 项目级技能（agent/platform 双仓工作流），共 10 个 |
| `user/` | `~/.agents/skills`（`~/.zcode/skills` 为软链） | 用户级通用技能，共 39 个 |
| `plugins/` | `~/.zcode/cli/plugins/cache`（zcode 官方插件） | 插件自带技能，共 3 个 |

同步口径：三个来源目录的**当前状态全量镜像**（非按使用次数精选）。`project/` 以 ZCode 侧为权威版本（WSL 环境）；`codex` 仅存在于 Claude 侧故从 `.claude/skills` 取；已剔除 `__pycache__/`、`*.pyc`、`.cache/` 运行缓存（见 `.gitignore`）。

## project/（10 个）

| 技能 | 用途 |
|---|---|
| aip-client-test | AIP 协议 Mock 客户端冒烟（认证→WS→语音轮次全链路） |
| codex | 在 Claude Code 里委托本机 Codex CLI 非交互执行 |
| skill-sync | Claude/ZCode 技能目录双向同步 |
| startup | 启动 gateway/admin-api/web 三件套开发服务 |
| stop-all | 停止三件套并验证端口释放 |
| story-flow-test | 故事引擎全流程自动化测试 |
| story-import | 故事图 JSON + 音频导入数据库三表 |
| test-chat | 机器人语音链路全流程测试（LLM+TTS） |
| worktree-cleanup | 清理已合入 main 的 git worktree |
| worktree-merge | 把 worktree 修改合并回指定分支 |

## user/（39 个）

ask-matt · chief-of-staff · claude-handoff · code-review · codebase-design · diagnosing-bugs · domain-modeling · git-guardrails-claude-code · grill-me · grill-with-docs · grilling · handoff · implement · implement-spec · improve-codebase-architecture · loop-me · migrate-to-shoehorn · pr · prototype · research · resolving-merge-conflicts · retro · scaffold-exercises · setup-matt-pocock-skills · setup-pre-commit · setup-ts-deep-modules · tdd · teach · to-questionnaire · to-spec · to-tickets · triage · wait-what · wayfinder · wizard · writing-beats · writing-for-agents · writing-fragments · writing-shape

## plugins/（3 个）

| 目录 | 来源插件 | 用途 |
|---|---|---|
| documents-docx | documents 0.1.7 | Word 文档处理 |
| skill-creator | skill-creator 0.1.0 | 创建/迭代技能 |
| zcode-guide-diagnosing-mcp | zcode-guide 0.3.0 | MCP 配置诊断 |

## 历史使用统计（2026-09 快照，归档时的精选口径）

ZCode 会话实际使用统计（窗口：2026-09-14 ～ 2026-09-23，76 次调用，131 个会话）：

| 次数 | 技能 | 位置 | 用途 |
|---|---|---|---|
| 22 | implement | user/ | 按 spec/ticket 实现 |
| 21 | code-review | user/ | 双轴代码审查 |
| 9 | diagnosing-bugs | user/ | bug/性能诊断 |
| 4 | startup | project/ | 启动 gateway/admin-api/web 开发服务 |
| 3 | aip-client-test | project/ | AIP 协议 Mock 客户端冒烟 |
| 2 | resolving-merge-conflicts | user/ | 解决 merge/rebase 冲突 |
| 2 | stop-all | project/ | 停止三件套开发服务 |
| 2 | skill-creator | plugins/ | 创建/迭代技能 |
| 2 | writing-for-agents | user/ | 面向 agent 的文档写作 |
| 2 | docx | plugins/（documents 插件） | Word 文档处理 |
| 1 | diagnosing-mcp | plugins/（zcode-guide 插件） | MCP 配置诊断 |
| 1 | to-tickets | user/ | 拆分 tracer-bullet 工单 |
| 1 | story-flow-test | project/ | 故事引擎全流程测试 |
| 1 | story-import | project/ | 故事数据入库 |
| 1 | skill-sync | project/ | Claude/ZCode 技能目录同步 |
| 1 | grill-me | user/ | 计划拷问 |
| 1 | grilling | user/ | 计划拷问 |
