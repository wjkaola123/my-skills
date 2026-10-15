---
name: prod-aip-test
description: 用 aip_client Mock 客户端直连生产环境（123.57.81.29:17005）做 AIP 链路验证——模拟设备走真实 认证→WS 会话→语音轮次（线上 ASR+灵魂合成+LLM+TTS 全链路），客户端事件与线上 gateway 日志对账。当用户说「测线上 / 测生产 / prod 冒烟 / 线上设备问答 / 测线上语音链路 / 线上 AIP」或改动合入后要验证已部署环境行为时使用本技能。本地 dev 环境的 AIP 冒烟用 /aip-client-test，不用本技能。
---

# 线上 AIP 冒烟（prod）

## 定位

对**已部署生产环境**跑 AIP 协议真实轮次，回答「线上到底什么行为」。与 `/aip-client-test` 的分工：那个测本地 dev（gateway:3000），本技能测线上（123.57.81.29:17005）。判定结论以**线上 gateway 日志为准**，客户端事件流只作辅助——线上链路慢，客户端等待窗口内常收不齐事件（见坑 1）。

## 固定事实（勿重新推导）

| 项 | 值 |
| --- | --- |
| 生产入口 | `http://123.57.81.29:17005`（web 容器 nginx 反代 → gateway:3000；WS 同源 `/aip/v1/session`） |
| 健康 | `GET :17005/gateway-api/health` 返回 `{"status":"ok"}` |
| 线上日志 | `ssh wujie@123.57.81.29 'grep "<device_id>" /opt/platform/logs/gateway/gateway.log'`（pino `ts` 是 UTC；按段拉取用 /pull-prod-logs） |
| 常备测试设备 | `aabbccdd0001`（name=claude-prod-smoke），secret 在本地 `/home/wj/project/.scratch/prod-aip-smoke-device.txt`（读法见骨架） |
| Python | `/home/wj/project/.venv-aip/bin/python`（requests/websockets/redis/psycopg2 已装） |
| 公共件 | 复用 `/aip-client-test` 的 `smoke_common.py`（connect/manual_turn/synth_frames/check…），DEV 常量指本地，须换线上 base_url + 生产设备 |

## 工作流

1. **前置**：不需要本地三件套服务（测的是线上）；venv、`/mnt/d/Tools/aip_client` 包、smoke_common 路径要可用。
2. **取设备**：默认用 `aabbccdd0001` + `.scratch/prod-aip-smoke-device.txt` 里的 secret。
   - 若 secret 文件丢失/设备被删：照 memory `prod-aip-smoke` 的「注册新设备」流程（admin 登录 `:17005/api/auth/login` → `POST /aip-admin/devices`，12 位 hex device_id + `openssl rand -hex 24`），secret 落回同一 scratch 文件，并把新 device_id 更新进本表。
3. **写脚本**：`/mnt/d/Tools/aip_client/_smoke_prod_<主题>.py`，骨架见下；断言 `check()`，结果 JSON 落同目录 `_smoke_prod_<主题>_result.json`。
4. **跑 + 对账**：脚本 PASS/FAIL 只作初筛；**判定前必查线上日志**（grep device_id，看 asr partial/final、灵魂合成、moderation、TX envelope）。
5. **汇报**：结论 + 两源证据各一句 + 结果 JSON 路径。线上是共享环境，跑完必须 `session_close()` 收干净。

## 脚本骨架

```python
import sys, time
sys.path.insert(0, '/mnt/d/Tools')
sys.path.insert(0, '/home/wj/project/.claude/skills/aip-client-test/scripts')
from smoke_common import *

RESULT = '/mnt/d/Tools/aip_client/_smoke_prod_<主题>_result.json'
SECRET = open('/home/wj/project/.scratch/prod-aip-smoke-device.txt').read().strip().split('=')[1]
DEV = dict(base_url='http://123.57.81.29:17005',
           device_id='aabbccdd0001', device_secret=SECRET)

c = connect(DEV)
r = manual_turn(c, '你好，请你做个自我介绍吧', 'zh', timeout=90)   # 线上要给足窗口
check('LLM 有回复', ...)
save_result(RESULT)
c.session_close(); c.ws.close()
```

现成范式：`_smoke_prod_qa.py`（2026-10-13 首跑脚本）。

## 纪律与坑

1. **客户端窗口 ≠ 链路好坏**：线上 ASR 流式 + 灵魂合成 + LLM 整链 ~15s 起，`manual_turn` 返回时 asr_final/agent_text/音频帧可能还没到——**别据此判 FAIL**，先查日志里 asr-tail（chars= 数）与 TX envelope 再下结论。要客户端收全事件就 `timeout=90` 并在 turn 后 `drain_messages`。
2. **别动真实设备**：线上 260 台设备密钥只存哈希，永远用自注册的测试设备；`aabbccdd0001` 是常备的，**跑完保留别删**。
3. **新设备首轮回复是「第一次醒来」口吻**（无画像无记忆，属正常），断言别依赖具体话术，认事件类型/日志标记。
4. **线上写库要克制**：测试设备 user_id=NULL，画像落设备 id；会影响线上数据表的用例（改 metadata/语言/画像）先问用户。
5. TTS 合成话术 lang 跟文本走（zh→Huihui / en→Zira），细节坑同 `/aip-client-test` 的纪律节。
