---
name: startup
description: 启动 agent 仓库的三个开发服务（gateway:3000 / admin-api:3001 / web:5173）并验证全部就绪；前置步骤会先确认 Docker 引擎和 dbca-postgres / dbca-redis 两个基础容器，再用 platform 仓最新源码重建 @dbca/shared、@dbca/db 并 overlay 进 agent 的 node_modules（不动 lockfile）。当用户要求"启动服务 / 把服务跑起来 / 重启服务 / start the dev servers / 启动 gateway、admin-api、web"时使用本技能；也适用于端口被占（EADDRINUSE）、服务已部分运行、改完 .env 需要重启等场景。
---

# 启动 agent 三件套（gateway / admin-api / web）

本技能部署在双仓工作区根（`/home/wj/project/.zcode/skills/startup`），操作对象在 `/home/wj/project/agent` 仓。

目标：先确保 Docker 引擎和两个基础容器（dbca-postgres / dbca-redis）活着，再用 platform 仓最新源码重建 `@dbca` 制品并 overlay 进 agent 的 node_modules，然后把三个开发服务拉起来，并用真实 HTTP 请求证明每个都活着，最后给出一张状态表。
不是"进程启动了"就算成功 —— 必须跑到"请求有响应"才算。

## 基础设施清单（固定事实，勿重新推导）

| 容器 | 镜像 | 端口 | 定义处 | 存活证据 |
|------|------|------|--------|----------|
| dbca-postgres | dbca-postgres-age:pg17 | 5432 | platform 仓库 `docker/docker-compose.yml` | `docker inspect` Health = healthy |
| dbca-redis | redis:7-alpine | 6379 | 同上 | 同上 |

本机 Docker 是 **WSL 内原生 systemd 服务**（`docker.service`，已 enabled 开机自启），不是 Docker Desktop——引擎没起时 `sudo systemctl start docker`，不要去找 Docker Desktop.exe。

## 服务清单（固定事实，勿重新推导）

| 服务 | 目录 | 命令 | 端口 | 存活证据 |
|------|------|------|------|----------|
| gateway | `/home/wj/project/agent/packages/gateway` | `npm run dev`（tsx watch，`--env-file=../../.env`） | 3000（`.env` 的 `PORT`） | `GET /api/health` 返回 `{"status":"ok",...}` |
| admin-api | `/home/wj/project/agent/packages/admin-api` | `npm run dev`（nest watch） | 3001（`ADMIN_API_PORT`） | `GET /api/agents` 返回 **401**（鉴权拦截 = 服务在响应；404/连接拒绝才是死） |
| web | `/home/wj/project/agent/packages/web` | `npm run dev`（vite） | 5173 | `GET /` 返回 200 HTML |

易错点：
- gateway 的健康检查路径是 **`/api/health`**，不是 `/health`（路由注册在 `/api` 前缀下，裸 `/health` 是 404）。
- admin-api 返回 401 是**好信号**（全局前缀 `api` + 鉴权中间件都在工作），不要把它当失败。
- admin-api 启动最慢（nest 编译约 15~25s），gateway 约 10s，web 不到 1 秒。验证轮询要按最慢的等。

## platform 制品清单（固定事实，勿重新推导）

gateway / admin-api 依赖的 `@dbca/shared`、`@dbca/db` 是 platform 仓（`/home/wj/project/platform`）发布的私有制品。启动前必须用 platform 当前源码重建并 overlay 到 agent 的 node_modules——本地惯例是**只同步制品目录、不动 lockfile**（CI/部署环境另需 platform 侧 catchup 发版，与本机无关）。

| 制品 | platform 源码位置 | overlay 内容（= 包的 files 字段） |
|------|------------------|----------------------------------|
| `@dbca/shared` | `packages/shared` | `dist/` + `package.json` + `openapi.json` |
| `@dbca/db` | `packages/db` | `dist/` + `drizzle/` + `package.json` |
| `@dbca/soul` / `@dbca/soul-gateway` | 源码**不在本机**（GitLab project 16 另一仓） | 不 overlay——只走 registry 安装（`npm install --allow-remote=all`） |

agent 将来新增对 `@dbca/api-client` 之类其他 platform 包的依赖时，把它加进 overlay 列表即可。

## 步骤

### 0. 前置：Docker 引擎 + 基础容器

gateway 和 admin-api 依赖 PG/Redis，引擎或容器没起来后面必然连不上，所以这一步必须先绿。

```bash
docker info >/dev/null 2>&1 && echo "engine up" || echo "engine down"
```

**引擎已就绪** → 直接进容器检查。**引擎未就绪** → 启动 systemd 服务并轮询等引擎：

```bash
sudo systemctl start docker
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 3; done
docker info >/dev/null 2>&1 && echo "engine ready" || echo "engine NOT ready（journalctl -u docker 查原因）"
```

引擎就绪后确保两个容器在跑。`docker start` 是快路径（容器已存在但停着）；容器不存在（首次/被 prune）时它报错，正好触发 compose 分支兜底创建：

```bash
docker start dbca-postgres dbca-redis 2>/dev/null \
  || docker compose -f /home/wj/project/platform/docker/docker-compose.yml up -d
```

两个容器都配了 healthcheck，等到 healthy 才算数（`Up (health: starting)` 不算，最长 60s）：

```bash
for i in $(seq 1 20); do
  pg=$(docker inspect -f '{{.State.Health.Status}}' dbca-postgres 2>/dev/null)
  rd=$(docker inspect -f '{{.State.Health.Status}}' dbca-redis 2>/dev/null)
  [ "$pg" = healthy ] && [ "$rd" = healthy ] && break
  sleep 3
done
docker ps --filter name=dbca-postgres --filter name=dbca-redis --format '{{.Names}}\t{{.Status}}'
```

超时未 healthy → `docker logs dbca-postgres` / `docker logs dbca-redis` 排查，不要带病进第 1 步。
本步幂等：引擎在跑、容器 healthy 时所有命令都是 no-op，重复执行无副作用。

### 1. 预检

```bash
ls /home/wj/project/agent/.env /home/wj/project/agent/node_modules/.bin/tsx >/dev/null 2>&1 && echo ready
ss -ltn | grep -E ":3000|:3001|:5173" || echo "ports free"
```

- `.env` 缺失 → 停下来让用户配置（gateway 靠它注入端口和 LLM/TTS 配置）；`node_modules` 缺失 → 先 `npm ci`（在 `/home/wj/project/agent` 下执行）。
- **端口已被占用时先判断是不是服务已经在跑**：直接执行第 4 步验证脚本，三绿就什么都不用启动，汇报现状即可（幂等，绝不重复拉起）。但注意：即便三绿，第 2 步的 platform 重建照做——只是做完 overlay 后需要重启服务才生效（见该步说明）。
- 端口被占但验证不绿（典型：上次残留的孤儿进程）→ 杀掉再启动：

```bash
# Linux/WSL：fuser 直接杀监听端口的进程（dev 进程是多级子进程，杀持 socket 的 node 即可）
fuser -k 3000/tcp 3001/tcp 5173/tcp 2>/dev/null
```

背景：这个仓库的 dev 进程是 `npm run dev` → tsx/nest 的多级子进程，持端口的是最内层 node；杀它外层 npm 会自然退出。这是本机实测踩过的坑（Windows 时代 taskkill //F //T 的等价操作）。

### 2. 重建 platform 制品并 overlay 到 agent node_modules（启动前必做）

服务进程只在启动时解析一次 node_modules（tsx watch 不监听 node_modules 变化），跑起来后再 overlay 不生效——所以这步必须在启动服务**之前**完成；对已运行的服务做完 overlay 必须重启。

```bash
# 1) 构建 platform（turbo 按依赖拓扑序编译 packages/*，产物在各包 dist/；冷构建约 20s，源码没变时命中 turbo 缓存秒级完成）
runuser -u wj -- npm --prefix /home/wj/project/platform run build

# 2) overlay 制品进 agent 的 node_modules（内容对应「platform 制品清单」；必须等上一步构建成功，失败时 dist 可能是半成品）
runuser -u wj -- rsync -a --delete /home/wj/project/platform/packages/shared/dist/ /home/wj/project/agent/node_modules/@dbca/shared/dist/
runuser -u wj -- rsync -a /home/wj/project/platform/packages/shared/package.json /home/wj/project/platform/packages/shared/openapi.json /home/wj/project/agent/node_modules/@dbca/shared/
runuser -u wj -- rsync -a --delete /home/wj/project/platform/packages/db/dist/ /home/wj/project/agent/node_modules/@dbca/db/dist/
runuser -u wj -- rsync -a --delete /home/wj/project/platform/packages/db/drizzle/ /home/wj/project/agent/node_modules/@dbca/db/drizzle/
runuser -u wj -- rsync -a /home/wj/project/platform/packages/db/package.json /home/wj/project/agent/node_modules/@dbca/db/

# 3) 验证：node_modules 版本追平 platform 且两个包都能 import（导出正常水位 shared≈719 / db≈378，0 或报错 = overlay 失败）
cd /home/wj/project/agent && runuser -u wj -- node -e "
const pk = (p) => require('/home/wj/project/agent/node_modules/@dbca/' + p + '/package.json').version;
const pf = (p) => require('/home/wj/project/platform/packages/' + p + '/package.json').version;
Promise.all([import('@dbca/shared'), import('@dbca/db')]).then(([s, d]) => {
  if (pk('shared') !== pf('shared') || pk('db') !== pf('db')) { console.error('版本未追平'); process.exit(1); }
  if (!Object.keys(s).length || !Object.keys(d).length) { console.error('导出为空'); process.exit(1); }
  console.log('overlay ok: shared@' + pk('shared') + ' (' + Object.keys(s).length + ' exports), db@' + pk('db') + ' (' + Object.keys(d).length + ' exports)');
}).catch(e => { console.error('IMPORT FAILED:', e.message); process.exit(1); });"
```

易错点：
- shell 是 root 时一切 npm/rsync 前缀 `runuser -u wj --`（root 写过的文件会让后续 wj 的构建/启动莫名失败）；会话本来就是 wj 则去掉前缀直接跑。
- `rsync --delete` 让 dist 完整镜像 platform 侧产物，防止改名的旧 .js 残留干扰解析。
- overlay 后 `npm ls @dbca/shared` 与 lockfile 版本可能不一致——预期现象，**不要**顺手改 lockfile。
- `@dbca/soul` / `@dbca/soul-gateway` 报 named export 缺失不是本步能修的：它们源码不在本机，判据是 lockfile 里的版本有没有该导出；node_modules 落后于 lockfile 时跑 `runuser -u wj -- npm install --allow-remote=all`（npm 12 必须带 flag）。

### 3. 启动（三个后台任务并行）

```
cd /home/wj/project/agent/packages/gateway   && npm run dev   # 后台
cd /home/wj/project/agent/packages/admin-api && npm run dev   # 后台
cd /home/wj/project/agent/packages/web       && npm run dev   # 后台
```

顺序无关紧要：gateway 的 admin-sync 每 8 秒左右重试，admin-api 后起来也能自动接上（日志出现 `[admin-sync] ok`）。

### 4. 等待 + 轮询验证

```bash
node /home/wj/project/.zcode/skills/startup/scripts/verify.mjs
```

脚本会轮询三个端点（默认最长 90 秒、每 3 秒一轮），全部就绪退出码 0 并打印状态表；超时退出码 1 并指出哪个没起来。刚启动时先 `sleep 20` 再跑可以少等几轮。

### 5. 联动确认（可选但推荐）

gateway 起初连不上 admin-api 会刷 `adminsync.degraded`，属预期；在 gateway 任务输出里确认最终出现：

```
[admin-sync] ok from http://localhost:3001/api  groups=NN agents=NN skills=NN
```

没有这行 = 三服务间联动断（机器人链路只有内置 seed 数据），需要查 `ADMIN_API_BASE` / `JWT_SECRET` 是否两侧一致。

### 6. 汇报

用上面的服务清单表格输出：服务、端口、状态（✅/❌）、证据（health JSON 摘要 / 401 / 200），并在表里附上第 0 步的 dbca-postgres / dbca-redis 两行（`Up X (healthy)` 即证据），最后附三个后台任务 ID（用户要停服务时直接 TaskStop + stop-all 技能兜底清理）。

## 已知无害告警（出现时不要追查、不要修）

gateway 启动日志里这些是缺可选配置的预期噪音，不影响三服务存活判定：
- `TUSHARE_TOKEN 未设置` → A 股 finance ingest 跳过
- `FINANCE_DSN_ENCRYPTION_KEY not set` → 金融数据源加密不可用
- Yahoo Finance 403 / Redis 版本低于 6.2 / opusscript pure-JS fallback
- `[RobotTtsStream] 401`（语音 TTS 缺 `DASHSCOPE_API_KEY`，文本链路不受影响）
