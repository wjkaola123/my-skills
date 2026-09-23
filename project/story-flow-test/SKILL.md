---
name: story-flow-test
description: AG-20 故事引擎全流程自动化测试 — 图驱动随机选决策边，动态合成语音答案，走真实 ASR+LLM 链路完成 SELECTING→PLAYING→ENDING 端到端验证。当用户提到故事引擎测试、故事全流程、story flow test、AG-20 验证、随机路径测试故事、决策点路由测试，或想验证 aip-ws-hub 故事链路（召回门/段链播报/语义路由/锚定/exit）是否工作时应使用本 skill。
---

# 故事引擎全流程测试（story-flow-test）

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/story-flow-test`），操作对象在 `/home/wj/project/agent` 仓；aip_client 工具与 TTS 缓存在 Windows 盘 `/mnt/d/Tools/aip_client`（powershell.exe 互通）。

模拟 W810 常开麦设备，从 PG 读故事图，**图驱动**随机选择决策边，动态合成所选边
`semantic_description` 的英文语音，走完整链路（paraformer ASR → LLM 召回门/语义路由 →
Redis 锚定 → PG 落盘 → exit）直到终点。不注入 ASR 文本——这是刻意的黑盒设计，
2026-08-28 的实测中它先后暴露了 turnState 卡死、LLM 3s 超时必挂、ASR IdleTimeout
丢帧三个真 bug，注入文本的测法一个都发现不了。

## 运行

```bash
PY=/home/wj/project/.venv-aip/bin/python     # 依赖装在该 venv（见前置检查 4）
$PY /home/wj/project/.zcode/skills/story-flow-test/scripts/story_flow.py             # 随机路径
$PY /home/wj/project/.zcode/skills/story-flow-test/scripts/story_flow.py --seed 42  # 可复现
```

常用参数：`--story 83`（本机 Magical Land；未导入时见 story-import 技能）、
`--device mock-w810-001 --secret mock-secret-123456`、
`--max-steps 25`（回环图兜底）、`--enter-keep`（跳过清场，续玩/调试）。

一次全流程 5-15 分钟（取决于随机路径落在哪些节点——节点音频 5s~190s 不等，
1.5x 实时发送）。输出：控制台断言矩阵 + `scripts/../story_flow_result.json`
（轨迹/断言，供 CI；在技能目录下，属运行产物不参与技能同步）。

## 前置检查（脚本失败先对照这里）

按顺序跑，任何一步失败先修再测：

1. `curl -s http://127.0.0.1:3000/api/health` → gateway 活着
2. `grep STORY_RECALL_GATE_MODE /home/wj/project/agent/.env` → 必须 `=active`
   （默认 off 时故事门整体关闭，"Tell me a story" 只会得到闲聊现编）
3. 故事数据：PG `stories` 表有 `status='published'` 行；
   `/home/wj/story-audio/story-nodes/{story_id}/` 下音频文件在盘（OPFR 格式）
4. Python 依赖：`requests websockets redis psycopg2`（装在 `/home/wj/project/.venv-aip`，
   缺则 `/home/wj/project/.venv-aip/bin/python -m ensurepip --upgrade && .../bin/python -m pip install requests websockets redis psycopg2`）
5. 音频合成链：powershell.exe 互操作 Zira(en-US) TTS + agent 仓 `/home/wj/project/agent/node_modules/opusscript`

## 输出解读

- **PASS/FAIL 矩阵**：每步 4 项断言（ASR 识别 / W810 start-end 严格交替 /
  媒体段帧数==ΣframeCount / current_node 锚定）+ 进入与终点专项
- **[轨迹]**：`n148-[Fight it!]->n149-[linear]->n151-...->n160` 形式的实际路径
- 已知**设计符合性差异**（不算 bug）：媒体段不发 `voice.tts.sentence` 整节点字幕
  （AG-20 时序图有、实现无，产品知悉项）；召回门 LLM 判 none 时放行闲聊现编
  （设计内兜底，脚本已用措辞变体重试绕开 LRU 缓存）

## 故障诊断（2026-08-28 实测沉淀的四个坑）

| 症状 | 根因 | 修法 |
|---|---|---|
| 决策点回答后永远"Sorry, I didn't catch that" | ① 故事 TTS 段后 turnState 卡 `downlink_tts`（speak 的 onTtsEnd 不恢复）② supervisor LLM `timeoutMs:3000` 必超时（真实 TTFT 3.7-5.3s） | ① aip-ws-hub.ts buildStoryVoicePort.speak 的 onTtsEnd 恢复 uplink（已修）② supervisor 三处改 8000（已修） |
| **改了 supervisor/src 不生效** | gateway 加载 `packages/supervisor/dist` 编译产物 | `npm run build -w @dbca/supervisor` 后再 touch gateway 重启 |
| 媒体段播完后说话没反应（ASR 无 final） | paraformer 服务端 ~60s 无帧 IdleTimeout 杀流，hub 惰性重建窗口期帧全丢 | 测试侧已用常开麦背景静音流规避（真机恒定发帧无此问题）；hub 重建丢帧仍是待加固点 |
| Redis 断言全 miss | 双栈双 Redis 各持一份（Windows 时代 `localhost`(=::1) 与 `127.0.0.1` 是两实例；WSL 下同理先确认 gateway 连的是哪个） | 脚本已双栈探测；手工查用 `cd /home/wj/project/agent/packages/gateway && node --env-file=../../.env` 调 `@dbca/db` 的 `getRedis()` |

gateway 日志（排查 ASR/LLM 行为的第一现场）：
`/home/wj/project/agent/packages/gateway/logs/gateway/gateway.log`
（需 `.env` 里 `GATEWAY_LOCAL_LOG_ENABLED=1`，重启生效）。关键 grep：
`ASR final`、`story track`、`task-failed.*IdleTimeout`、`llm_ttft_ms`。

## 边界与注意

- 仅适用本地联调栈（gateway 3000 / PG agent_cluster / Redis 6379 / `/home/wj/story-audio`）。
  换环境时改脚本头部常量（BASE_URL/MEDIA_DIR/AGENT_DIR/设备凭据）。
- 召回门与退出正则**只认英文**（2026-08-27 决议）——测试话语必须是英文。
- `classifyStoryRecall/Route` 按原文 LRU 缓存首次判定：同句重试无效，
  必须换措辞（脚本已内置变体池）。
- 随机路径可能走进图内回环（旧 Windows 库 story 82 实测有 155→156→152 回边）——这是特性不是
  bug：正好覆盖 `cycle-guard` 构建期防环；`--max-steps` 兜底死循环。
- 断言"媒体段帧数"对 TTS fallback 节点（audio_file 缺失走 CosyVoice 读 content）
  会 FAIL——那不是预录帧窗口。本机 Magical Land（sid=83）导入时全节点有音频，
  不触发；若导入的新故事有无音频节点，属预期行为差异。
