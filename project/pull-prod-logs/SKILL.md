---
name: pull-prod-logs
description: 拉取生产服务器（123.57.81.29）gateway 日志切片到本地 .scratch/logs/ 并做健康体检。自动处理日志 UTC 时间戳换算、按行号切片、gzip 传输。当用户说"拉日志 / 拉服务器日志 / 拉生产日志 / 拉一下今天下午的日志 / gateway 日志 / prod logs"，或排查线上问题需要看生产日志时使用本技能。
---

# 拉取生产 gateway 日志

本技能部署在工作区根（`/home/wj/project/.zcode/skills/pull-prod-logs`），目标：把指定本地时间段的生产 gateway 日志切片拉到本地，给出健康概况和异常清单。

## 固定事实（勿重新推导）

| 项 | 值 |
|------|------|
| 服务器 | `ssh wujie@123.57.81.29`，仅公钥；连不上 22 端口 = 出口 IP 漂移，找管理员更新安全组 |
| 日志目录 | `/opt/platform/logs/gateway/`（属主 ops，wujie 可读） |
| 当前文件 | `gateway.log`（轮转上限 ~51M，写满滚为 `gateway.1.log`、`gateway.2.log` …，数字越大越旧） |
| 保留窗 | 约 14.6 天，更早的取证走数据库 |
| 本地输出 | `/home/wj/project/.scratch/logs/<YYYYMMDD-HHMM>-<HHMM>/` |

**UTC 坑**：服务器系统时区是 +0800，但日志 `ts` 是 **UTC**（慢 8 小时）。"今天下午 12:00" = UTC `T04:`。脚本已处理换算，手工 grep 时必须自己减 8。

**日志三族格式**（同一文件混排）：

| 格式 | 样例 | 时间戳 | 能否按时间过滤 |
|------|------|------|------|
| 结构化事件 | `{"ts":"2026-10-12T04:28:14.893Z","type":"llm.call",...}` | UTC ISO 字符串 | ✅ |
| pino HTTP | `{"level":30,"time":1791779295133,...}` | epoch 毫秒 | 仅手工换算 |
| 文本/多行 | `[AipHub/...] RX text: ...`、soul 人格多行输出（anchors、thought_core 溯源等） | 无 | ❌ 跟随切片带出 |

## 步骤

### 1. 确认时间范围，跑脚本

时间一律说**北京时间**，脚本内部换算 UTC、定位行号、压缩传输：

```bash
# 今天下午：
/home/wj/project/.zcode/skills/pull-prod-logs/scripts/pull-gateway-log.sh "12:00"
# 指定起止（结束只影响本地裁剪与命名）：
/home/wj/project/.zcode/skills/pull-prod-logs/scripts/pull-gateway-log.sh "2026-10-12 14:00" "16:00"
# 更早范围：先估天数选轮转文件（当前 gateway.log 每天约 8-10M）
/home/wj/project/.zcode/skills/pull-prod-logs/scripts/pull-gateway-log.sh "2026-10-08 00:00" "23:59" gateway.1.log
```

- 脚本幂等可重跑（同范围输出到同一目录覆盖）；退出码 0=成功，2=范围内该文件无 JSON 行（换轮转文件），3=SSH 失败。
- 脚本先找第一条 `ts >= 开始` 的 JSON 行号再 `tail -n +N | gzip`；起点前的无时间戳文本行不带上（误差几行，可接受）。
- `.gz` 是裁剪前的原件（到拉取时刻 EOF）；`.log` 已按结束时间裁剪。

### 2. 本地体检

对拉下来的 `.log` 按序跑（均在文件所在目录执行）：

```bash
f=gateway.log
# 覆盖范围
grep -o '"ts":"[^"]*"' $f | sed -n '1p;$p'
# 三族格式计数
awk '{if ($0 ~ /^{"ts":/) c["json-ts"]++; else if ($0 ~ /^{"level"/) c["pino"]++; else if ($0 ~ /^\[/) c["bracket"]++; else c["other"]++} END{for (k in c) print k, c[k]}' $f
# pino HTTP level 分布（必须 ^{"level" 锚定——"level":50 会误匹配 body 里的 params.level=50）
grep '^{"level"' $f | grep -o '"level":[0-9]*' | sort | uniq -c
# 真实报错按 type 分组（必须 '"error":"' 带引号——不带引号会把 "error":null 全算进去）
grep '"error":"' $f | grep -o '"type":"[^"]*"' | sort | uniq -c | sort -rn
# 事件 type 分布 top20
grep -o '"type":"[^"]*"' $f | sort | uniq -c | sort -rn | head -20
```

值得单独看的 type：`aip.turn.stuck`（轮次卡住）、`voice.degraded`（链路降级）、`turn.deadline`（45s 硬限）、`aip.auth`（看 `outcome` 分布）、`memory.extract` / `llm.call`（LLM 链路报错）。

### 3. 汇报

按此结构：**拉取结果**（文件路径、行数、覆盖时间范围——本地+UTC 都给）→ **健康概况**（HTTP level、auth 成败、主链路 main 调用成败）→ **异常清单**（按严重度排，每条给首次出现时间、影响面、证据行）。异常结论保留不确定度：相关不等于因果。

## 分析坑清单

- **错误明细被截断**：`llm.call` 行内 `"error"` 字段写入时截断，`code` 字段可能看不到——看行尾用 `rev | cut -c1-350 | rev`。
- **错误文案可跨 type 关联**：同一根因（如阿里云欠费 Arrearage "Access denied... good standing"）会在 `llm.call` 和 `memory.extract` 同时出现，按 message 文案 grep 而不是只按 type。
- **purpose 字段区分影响面**：`llm.call` 的 `purpose=main` 是主对话、`other`/`extractor` 是辅助链路——辅助链路报错不代表主链路挂了，分开统计再下结论。
- **多行文本体积大**：soul 人格/记忆多行输出可占行数 80%+，且无时间戳，统计"行数"时先分格式再解读。
- **`.scratch/logs/` 不进 git**（`.scratch` 是工作区约定目录），取证文件放这里即可。

## 关联

- 服务器访问细节（PG 只读库隧道连法、安全组流程）：memory `dbca-prod-server-access`
- 需要复现链路问题时：`/aip-client-test`（mock 设备冒烟）依赖本地环境，与本技能（生产取证）互不替代
- 深度下钻可委托 `/codex` 并行分析大文件
