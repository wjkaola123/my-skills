---
name: aip-client-test
description: 使用 /mnt/d/Tools/aip_client 的 AIP 协议 Mock 客户端，把用户口述的测试意图落成可复跑的冒烟脚本并真机执行——模拟 mock 设备走完 认证→WS 会话→语音轮次（真实 ASR+LLM+TTS 全链路），证据三源对账（客户端事件流 / gateway.log / PG 画像）。当用户说「用 aip_client 测一下 / 模拟设备说某句话 / AIP 冒烟 / 测语音轮次 / 测儿童审核 / 测媒体上传 / 测传感器上报 / mock 设备对话」或要求对 agent 仓 gateway 改动做 AIP 链路端到端验证时应使用本技能。
---

# AIP 客户端冒烟测试（aip_client）

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/aip-client-test`），操作对象在 `/home/wj/project/agent` 仓；aip_client 工具本体在 Windows 盘 `/mnt/d/Tools/aip_client`（WSL 互通）。

## 定位

用户用自然语言描述一个测试意图（「测一下儿童模式说脏话会不会被拦」「持续流模式说退出故事会怎样」「媒体上传走 HTTP 直传对不对」），本技能把它落成 `/mnt/d/Tools/aip_client/_smoke_<主题>.py` 冒烟脚本，跑真实链路，按三源证据给 PASS/FAIL 结论。**测的是已部署的 gateway 行为，不是改 agent 仓代码**——发现 bug 报告给用户，不要顺手改。

## 工作流

1. **前置检查**：三件套 dev 服务必须就绪（gateway:3000 / admin-api:3001 / web:5173，没跑用 startup 技能拉起）；Docker 里 dbca-postgres / dbca-redis 活着；`node -v` 和 Python 可用（合成链依赖）。Python 依赖（`requests websockets redis psycopg2 opuslib`）装在工作区根 `.venv-aip`（`/home/wj/project/.venv-aip/bin/python`）——该 venv 目前是空壳（无 pip 无包），首次使用先补齐：`/home/wj/project/.venv-aip/bin/python -m ensurepip --upgrade && /home/wj/project/.venv-aip/bin/python -m pip install requests websockets redis psycopg2-binary opuslib`。
2. **理解意图 → 定用例**：把用户描述拆成可断言的检查项（说了什么话、期望哪条链路生效、在哪看证据）。话术样本要贴生产真实说法，别造没人会说的句子。
3. **写脚本**：存到 `/mnt/d/Tools/aip_client/_smoke_<主题>.py`，开头接 smoke_common（见下方骨架），断言用 `check(name, cond, detail)`，结果落 `/mnt/d/Tools/aip_client/_smoke_<主题>_result.json`（累加式）。
4. **跑 + 对账**：执行脚本；PASS/FAIL 拿不准时开三源证据交叉（见下）。
5. **汇报**：`N/M 过`+ 失败项的 detail + 指出结果 JSON 路径。测试写过画像的，跑完清档。

## 脚本骨架（最小可跑）

```python
import sys
sys.path.insert(0, '/mnt/d/Tools')                                            # aip_client 包
sys.path.insert(0, '/home/wj/project/.zcode/skills/aip-client-test/scripts')  # 公共件
from smoke_common import *

RESULT = '/mnt/d/Tools/aip_client/_smoke_<主题>_result.json'

c = connect(DEV1)                       # authenticate → connect → session_open
off = log_size()                        # turn 前记 gateway.log 偏移
r = manual_turn(c, '今天天气怎么样', 'zh')   # 或 continuous_turn（生产主形态）
check('ASR 识别正确', '天气' in str(r.get('asr_final', '')), r)
lines = log_read(off)
events, hub = parse_log_events(lines, DEV1['device_id'])
save_result(RESULT)
c.session_close(); c.ws.close()
```

公共件 `smoke_common.py`（本技能 `scripts/` 下；Claude Code 侧的双拷贝在 `/home/wj/project/.claude/skills/aip-client-test`，改动需两侧同步——见 skill-sync 技能）提供：DEV1/DEV2/USER1/USER2 常量、`synth_frames`（TTS→wav→opus 帧，md5 缓存，重复句子零成本；WSL 下经 powershell.exe 互操作系统语音）、`load_frames`、`connect`、`manual_turn` / `continuous_turn`、`log_size/log_read/parse_log_events`、`pg/get_profile/clear_profile/wait_profile`、`check/save_result`。

## 客户端能力速查（client.py，54KB，按需读源码）

| 域 | 方法 |
|---|---|
| 连接 | `authenticate / do_refresh / connect / session_open / session_ping / session_close / session_resume` |
| 语音 | `voice_turn(文件)` `voice_turn_continuous(文件, tail_silence_sec)` `voice_turn_silence / voice_turn_cancel / voice_turn_with_media`；帧级自控走 `manual_turn`/`continuous_turn`（smoke_common） |
| 媒体 | `media_upload`（分片+直传）`media_inline` `media_vision_request` `media_download / media_download_range / media_head` `display_ack` `trigger_display` |
| 传感器/设备 | `sensor_event / sensor_subscribe / sensor_unsubscribe / sensor_snapshot / send_sensor_batch` `trigger_invoke / device_invoke_result / wait_for_device_invoke` |
| 工具 | `drain_messages` `wait_for_type(type)`（等特定信封）`run_interactive`（人工探索） |

协议细节（信封类型/二进制帧格式）读 `/mnt/d/Tools/aip_client/16-AIP协议说明.md`。

## 故事用例：全流程走最快路径，非全流程取前缀（Magical Land Ch.1，story_id=83）

**先按测试目的定覆盖范围，效率最大化**：只有需要**全流程覆盖**（起→终完整链路验证）时才从头跑到结局，此时不随机选边，固定走全图 16 条起→终路径中音频最短的一条（2026-09-17 按音频头实测）；测试目的不要求跑完全程的（只验某决策点路由 / 召回门 / 接管 / 退出等），**取路径前缀推进到目标检查点，断言完即收**（exit 或关会话），不把余下段落播完：

| 连播段 | 节点 | 音频 | 累计 | 段末 |
|---|---|---|---|---|
| 段1 | #161 n_start | 50.0s | 50.0s | 决策1 |
| 段2 | #163 + #164 | 99.1s | 149.1s | 决策2 |
| 段3 | #165 | 111.1s | 260.2s | 决策3 |
| 段4 | #171 + #173 结局 | 280.5s | 540.7s | 自动收尾 |

- 全流程：纯音频 540.7s ≈ 9 分钟；加 3 次决策交互（问题 LLM 生成 + TTS 播报 ~10–18s、答句、ASR + 语义路由），**自动化全程约 9.5–10 分钟**；该故事任意路径 ≤ ~15 分钟，**全流程用例超时预算留 15–20 分钟**。段播完才提问，答句等提问播完再发。
- 前缀用例耗时 ≈ 累计列 + 途经的每个决策交互 ~15–25s。例如只测决策 2 的语义路由：段1 + 答决策1 + 段2 + 答决策2 后断言路由结果即收，**~3 分钟级**，不必跑到结局。
- 决策答句（语义路由不必逐字，用原句最稳；en 文本 → 合成 lang='en'）：
  1. `Run away fast!`（→#163；选 Fight it! 多 23.8s）
  2. `Let's take a careful look`（→#165；listen 支线绕远）
  3. `Tie up the bear with vines!`（→#171；选 rock 多 28.7s）
- 故事数据变更后重算时长：音频在 `${MEDIA_LIBRARY_DIR}/story-nodes/83/`（本机 `/home/wj/story-audio`），时长 = .opusframes 头（大端）frameMs@off8 × frameCount@off12。
- 图驱动随机路径的引擎级验证走 story-flow-test 技能；本节固定最快路径用于本技能的冒烟用例。

## 证据三源

1. **客户端事件流**：`_wait_voice_response` 返回的 dict + `wait_for_type` 收到的信封（asr_final / agent_text / emotion / timing）。
2. **gateway.log**：`/home/wj/project/agent/packages/gateway/logs/gateway/gateway.log`（smoke_common 内即此绝对路径）——pino JSON 行（按 `type` 前缀过滤，如 `moderation.*`）+ `[AipHub/<device_id>]` turnLog 原始行。**用偏移增量读**，别整读。
3. **PG**：`robot_user_profile`（画像/儿童标记）等表，DSN 在 smoke_common。写画像的用例跑前 `clear_profile(USER1)` 清档。

## 纪律与坑

- **话术合成 lang 跟文本走**（zh→Huihui / en→Zira），与设备语言设置解耦——ASR 双语都认。设备语言影响的是回复侧话术（mock-w810-001 现为 en-US）。
- **manual vs continuous**：manual 轮次确定性断言用；**持续流是生产主形态**（无 turn.start/end，尾部静音 VAD 断句），投机执行只在持续流活跃。
- **pino 事件的设备字段两套命名**：`moderation.*` 是 camelCase `deviceId`，`aip.auth` 等是 `device_id`——过滤时两个都试。
- **写库用例必须清档**：mock 设备是共享资源，画像残留会串到别的测试（儿童标记尤其）。
- 现有 `_smoke_*.py`（voice/story/moderation/auth_edge/media_edge/sensor 等）是现成范式，写新用例前先看最接近的一个。
- 脚本里别 `import opuslib` 之外的路径假设——opus 动态库由包 `__init__.py` 自动注入查找路径（Windows 侧 opus.dll；WSL 侧若缺 opus 共享库，相关用例 SKIP 而非报错）。
