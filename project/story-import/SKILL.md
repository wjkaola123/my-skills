---
name: story-import
description: 将新故事的原始数据（故事图 JSON + 已转码 .opusframes 预录音频）全流程导入数据库三表 stories/story_nodes/story_edges 并落盘音频到媒体库，含引擎校验、DB↔磁盘对账、Jira 记录。当用户要求"导入故事 / 故事数据入库 / 把新故事导进数据库 / 导入剧本 / 故事音频落盘 / story import / 再导一本故事 / 导入系列章节"时使用本技能。输入是 v3 故事图导出 JSON（title/language/start_node/nodes[].options[].target 结构，Magical Land 一族的格式；系列章节素材带 series+chapter 成对声明，AG-62）。
---

# 故事导入全流程（JSON 图 + 预录音频 → DB 三表 + 媒体库）

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/story-import`），操作对象在 `/home/wj/project/agent` 仓和 PostgreSQL。配套校验脚本 `scripts/verify-story-audio.mjs`（见 Phase 4）。

目标：一本新故事从原始材料到**可玩状态**——`stories / story_nodes / story_edges` 三表就位且过引擎校验器，预录 `.opusframes` 按 DB `audio_file` 引用逐个落盘可解析。数据行与音频落盘**可以分两步交付**：音频未落盘前运行时自动 TTS fallback 读 content（决议㉒），所以"先入库后补音频"是合法中间态。

## 固定事实（勿重新推导）

| 项 | 值 |
|---|---|
| 导入工具 | `/home/wj/project/agent/scripts/import-story.mjs`（吃 gateway dist 的校验器，跑前需 `npm run build -- --filter=@dbca/gateway`，在 agent 仓根执行） |
| DSN | `/home/wj/project/agent/.env` 的 `DATABASE_URL`（dev：`postgresql://agent:dev_password@localhost:5432/agent_cluster`） |
| 媒体库根 | `.env` 的 `MEDIA_LIBRARY_DIR`（本机现钉死 `/home/wj/story-audio`；未配置时代码默认 `./data/media-library` 按 gateway cwd 解析——勿依赖） |
| audio_file 约定 | `story-nodes/{sid}/{本地节点id}.opusframes`（2026-08-28 起从数字 nid 改本地 id：重导数据库 id 漂移、本地 id 跟剧本走；引擎只透传路径不解析文件名） |
| semantic_description 约定 | 决策点（≥2 出边）= option.text 必填（决议⑳）；单出边存 NULL（"» Continue" 是 UI 档口不是语义） |
| DB 容器 | `dbca-postgres`（platform 仓 `docker/docker-compose.yml`，端口 5432，healthcheck） |
| Docker | WSL 内原生 systemd 服务 `docker.service`（enabled 开机自启）；引擎没起 `sudo systemctl start docker` |
| 音频转码参数 | 16kHz / 单声道 / 60ms 定长帧 `.opusframes` 容器（pacer 恒 40ms 吐帧，60ms=1.5×实时设计值；**其他帧长本地测不出问题、真机必断续**） |
| Jira | AG 项目；AG-31 是本导入能力的票（决策记评论） |

## 输入格式（v3 故事图 JSON）

```jsonc
{
  "title": "...", "language": "en",
  // 系列章节（AG-62/0045）：series 与 chapter **成对声明**，都不带 = 单本
  "series": "Magical Land", "chapter": 2,
  "description": "一句话简介（语音菜单素材，可省）",
  "start_node": "n_start",
  "nodes": [
    { "id": "n_start", "text": "…", "audio_file": "n_start.mp3",
      "terminal": false,
      "options": [ { "text": "Fight it!", "target": "n_2a_fight", "condition": "" } ] }
  ]
}
```

- `language` **必填**（zh/en/zh-CN/en-US，归一化到 zh-CN/en-US）——0036 起 N-27 第一道防线，英文书漏标会静默进中文候选集。
- `series`/`chapter`（AG-62）：系列名（≤50 字符）+ 正整数章号，落 `stories.series_title/chapter`；有 series 无 chapter（或反之）→ 导入期即拒（成对声明约束，0045 CHECK 的前置）。同 `(series_title, language, chapter)` 重复导入被拒（唯一索引，见 Phase 2 两把锁）。
- `nodes[].id` 本地节点 id：**必须 `[A-Za-z0-9._-]+`**（进 audio_file 文件名，导入脚本会校验）。
- `terminal: true` → `is_end`；图级 `start_node` → `is_start`（唯一）。
- `state_flags`（图级声明）/ `set_flags`（节点级置位）/ `options[].condition`（`<flag> == true|false`）自 0035 起**入库**（`state_flags`/`set_flags`/`condition` jsonb 三列）。
- **素材双轨**：图 JSON 的 `audio_file` 字段引用的是 **TTS 源 mp3 名**（仅供对照，不是落盘物）；真实交付素材是**已转码的 `.opusframes`**（外部管线产出，按节点本地 id 命名，附 `conversion-report.json` 记录帧数）。
- 导入脚本**丢弃**：`model`、`segments`（TTS 角色/情绪台词）、节点 `title`、`auto`（自动推进由引擎按出边数决定）。
- 硬约束（DDL 挡）：`(title, language)` 唯一 varchar(50)；`(series_title, language, chapter)` 唯一（系列内）；每故事唯一 `is_start`；`is_start` 与 `is_end` 互斥。

## Phase 0 — 前置检查

```bash
# ① Docker 引擎 + dbca-postgres（healthy 才算活）
docker info >/dev/null 2>&1 || sudo systemctl start docker
# 等引擎就绪后：
docker compose -f /home/wj/project/platform/docker/docker-compose.yml up -d postgres
docker inspect -f '{{.State.Health.Status}}' dbca-postgres   # 期望 healthy

# ② 校验器 dist 在不在（导入脚本吃 dist，不在就 build）
ls /home/wj/project/agent/packages/gateway/dist/story/story-validate.js 2>/dev/null \
  || (cd /home/wj/project/agent && npm run build -- --filter=@dbca/gateway)

# ③ .env 两根变量在位
grep -E "^(DATABASE_URL|MEDIA_LIBRARY_DIR)=" /home/wj/project/agent/.env
```

## Phase 1 — 审图（写库前把问题看清楚）

```bash
cd /home/wj/project/agent
node scripts/import-story.mjs --in <故事图.json> [--audio <音频素材目录>] --dry-run
```

`--audio` 缺省取 `--in` 同级 `opusframes/`。dry-run 做形状校验（title/language/series+chapter 成对/节点 id 合法/悬边/state_flags 类型）+ 音频齐备性对照（`.opusframes` 按 JSON `audio_file` 源名换 `.opusframes` 后缀查）。重点看三类问题：

1. **opusframes 缺名/变体**：`节点 n_6a 缺 n_6a.opusframes` ——人工裁定哪个变体对号（Magical Land 先例：用 nowater 版），备用变体改名或移走。
2. **目录多余文件**：图未引用的音频（双版本/旧版）——移走，保持目录与节点 1:1。
3. **series/chapter 成对**：`声明了 series "X" 但缺 chapter`——素材侧补齐再导，导入期即拒不留半截。

节点数 × options 数 = 边数，心里有数再导（Magical Land 第一章：13 节点/18 边/6 决策点）。

## Phase 2 — 入库（单事务，可重跑）

```bash
cd /home/wj/project/agent
node scripts/import-story.mjs --in <故事图.json> --status published [--force]
```

- 工具自动做：形状校验 → 事务内插 stories/nodes/edges（audio_file 直写 `story-nodes/{sid}/{本地id}.opusframes`，决策点写语义/单出边 NULL）→ COMMIT 后回读真行过 `validateStory` → 失败删除刚写的（整单回滚不留坏剧本）→ 事务外拷音频（保证"库里有的音频一定在"）。
- 查重**两把锁**（导入期点名报错）：`(title, language)`（0036，同名跨语种合法共存）+ 系列素材的 `(series_title, language, chapter)`（0045，同系列同语种同章号至多一本）。
- 幂等：撞锁默认**拒绝**；`--force` 删冲突旧故事重建（CASCADE 清节点/边/用户进度 = 重置存档；两把锁命中的都删）。
- ⚠ `--force` 后 **story id 会变**（自增），旧音频目录成孤儿（脚本会提示路径，手工清理）。嫌烦就把 `--force` 留给开发期，内容修正式重导前先想清楚。
- `status=published` 才会被书目发现（`listPublished`）；草稿期可 `--status draft` 再 UPDATE。

## Phase 3 — 音频落盘（可后补，补齐前 TTS fallback）

**主路径（素材已是 .opusframes，常规情况）**：只剩两步——

1. **改名对号**：`{本地节点id}.opusframes`（与 DB audio_file 文件名一致；变体名如 `n_6a_vine_nowater.opusframes` 在此步归一为 `n_6a.opusframes`，Phase 1 的 warn 就是清单）。
2. **放置**：`$MEDIA_LIBRARY_DIR/story-nodes/{sid}/`。当前即 `/home/wj/story-audio/story-nodes/{sid}/`。

转码参数 16kHz/单声道/60ms 定长帧 `.opusframes` 容器由上游管线保证，`conversion-report.json` 随行（Phase 4 帧数对账用）。

**兜底（只拿到裸 mp3 时才转码）**：必须复用 `import-media.mjs` 同款编码链（gateway dist 的 `encodeOpusFrames` + `Opus16kEncoder`），勿自写容器格式。⛔ pacer 恒 40ms 吐帧，只有 60ms 帧长 = 1.5× 实时设计值；**错误帧长本地完全测不出，真机必断续**。

⚠ **别走错门**：`packages/gateway/data/aip-media/story/` 是**另一条业务线**（`/app/v1` 推送通道的整本合成缓存，uuid 命名），与故事引擎的 `story-nodes/` 命名空间无关；`import-media.mjs` 的 `--kind story` 写 manifest，引擎不读（kind='story' 已随 AG-20 裁定③退役）。也**别拿 v3-flash 里的 mp3 当落盘物**——那是 TTS 源/试听副本，重编码既有损又容易踩帧长参数。

## Phase 4 — 对账（交付门：不过不算完）

```bash
node /home/wj/project/.zcode/skills/story-import/scripts/verify-story-audio.mjs --story <sid>
# 或按标题：--title "Magical Land Chapter One"
```

通过标准（脚本 exit 0）：DB 该故事每行 `audio_file` 按 `MEDIA_LIBRARY_DIR` 解析命中文件、`decodeOpusFrames` 可解析、帧数>0，且 `story-nodes/{sid}/` 无对应不到 DB 的孤儿文件。有 conversion-report 就一并核对帧数。

## Phase 5 — 记录（Jira）

- 导入结果 + 命名裁定 + 音频缺口记到对应票的**评论**（AG-31 或新故事的票）。⛔ 禁止 MCP 整段回写工单正文（有损 markdown 往返会毁表格，见工作区记忆 jira-mcp-description-roundtrip-hazard）。
- 要挂新子任务时：`jira_create_subtask` 在 AG 项目会报 "Resource not found"（工具不发 reporter）——用 `jira_create_issue` + issueType=Subtask + customFields 显式给 `parent` 和 `reporter`（先例 AG-31）。

## 易错点（全部真实踩过）

- **Docker 引擎冷启动要等**：`systemctl start docker` 到 `docker info` 可用一般几秒，但 WSL 发行版本身刚启动时更慢；5432 ECONNREFUSED 先查容器别查代码。
- **gateway dev 改 .env 后要重启**：`tsx --env-file` 只在启动时读；改 `MEDIA_LIBRARY_DIR` 不重启 = 读旧值。
- **平台注释仍是旧约定**：`0034_story_tables.sql` 与 platform `schema/story.ts` 注释写的是数字 nid 格式（审计 2026-08-28 确认引擎行为无依赖，纯文档漂移）——以本 skill 的本地 id 约定为准。
- **音频先入库后落盘**：中间态全部节点走 TTS fallback 是**设计内行为**（㉒），不是故障。
- **素材主形态是 .opusframes**：mp3（如 v3-flash 目录里的）是 TTS 源/试听副本，仅作对照；落盘物永远是上游管线产出的 .opusframes（16k/mono/60ms + conversion-report.json）。
- **决策点语义缺失在导入期就被拒**（⑳ error 级）：图 JSON 里 ≥2 options 的节点每个 option.text 都不能空。
