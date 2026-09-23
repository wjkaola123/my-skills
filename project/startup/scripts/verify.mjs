#!/usr/bin/env node
/**
 * startup 技能的验证脚本：轮询 gateway / admin-api / web 三个端点，
 * 全部就绪退出码 0，超时退出码 1。端口从仓库根 .env 读取（与各服务
 * 的解析规则一致），缺省 3000 / 3001 / 5173；进程环境变量优先于 .env。
 *
 * 用法：node /home/wj/project/.zcode/skills/startup/scripts/verify.mjs [--timeout 90] [--quiet]
 *
 * 探测用 node:http 而非 fetch：undici 的 keep-alive 连接池在 Windows 上
 * process.exit 时偶发 libuv 断言（"UV_HANDLE_CLOSING"，实测 Node 24），
 * 会把退出码污染成 127/134。
 */
import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const timeoutIdx = args.indexOf('--timeout');
const timeoutSec = timeoutIdx >= 0 ? Number(args[timeoutIdx + 1]) || 90 : 90;
const quiet = args.includes('--quiet');

// scripts/ → startup/ → skills/ → .zcode/ → 工作区根（/home/wj/project），再拼 agent 仓
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'agent');
const env = { ...process.env };
const envPath = resolve(root, '.env');
if (existsSync(envPath)) {
  for (const line of readFileSync(envPath, 'utf8').split(/\r?\n/)) {
    if (line.trim().startsWith('#')) continue;
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m && env[m[1]] === undefined) env[m[1]] = m[2].trim();
  }
}

// admin-api 的端口回落逻辑与 packages/admin-api/src/main.ts 保持一致：
// ADMIN_API_PORT 优先；PORT≠3000 时可用；最终默认 3001。
const gwPort = Number(env.PORT) || 3000;
const adminPort =
  Number(env.ADMIN_API_PORT) ||
  (Number(env.PORT) && Number(env.PORT) !== 3000 ? Number(env.PORT) : 3001);
const webPort = 5173;

/** GET 一个 URL，返回 {status, body}；任何错误返回 null。 */
function get(url, timeoutMs = 4000) {
  return new Promise((resolvePromise) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => resolvePromise({ status: res.statusCode, body: data }));
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolvePromise(null));
  });
}

/** 单次探测：返回 null（未就绪/网络错误）或描述性证据字符串。 */
async function probe(url, check) {
  const res = await get(url);
  if (!res) return null;
  try {
    return (await check(res)) || null;
  } catch {
    return null;
  }
}

const services = [
  {
    name: 'gateway',
    url: `http://localhost:${gwPort}/api/health`,
    // 注意是 /api/health；裸 /health 是 404（路由挂在 /api 前缀下）
    check: (res) => {
      if (res.status !== 200) return null;
      const j = JSON.parse(res.body);
      if (j.status !== 'ok') return null;
      return `status=ok uptime=${Math.round(j.uptime)}s agents=${j.agents?.totalAgents ?? '?'}`;
    },
  },
  {
    name: 'admin-api',
    url: `http://localhost:${adminPort}/api/agents`,
    // 401 = 服务在响应且鉴权在工作（好信号）；200 也算活；404/5xx = 死
    check: (res) =>
      res.status === 401 || res.status === 200
        ? `HTTP ${res.status}（鉴权拦截=存活）`
        : null,
  },
  {
    name: 'web',
    url: `http://localhost:${webPort}/`,
    check: (res) => (res.status === 200 ? 'HTTP 200（vite 页面）' : null),
  },
];

const deadline = Date.now() + timeoutSec * 1000;
const evidence = new Map();
let round = 0;

while (Date.now() < deadline) {
  round++;
  const pending = services.filter((s) => !evidence.has(s.name));
  const results = await Promise.all(
    pending.map(async (s) => [s.name, await probe(s.url, s.check)]),
  );
  for (const [name, ev] of results) if (ev) evidence.set(name, ev);

  if (!quiet) {
    const state = services
      .map((s) => `${s.name}:${evidence.has(s.name) ? '✅' : '…'}`)
      .join('  ');
    console.log(`[verify] round ${round}  ${state}`);
  }
  if (evidence.size === services.length) break;
  await new Promise((r) => setTimeout(r, 3000));
}

console.log('');
const failed = services.filter((s) => !evidence.has(s.name));
for (const s of services) {
  const ev = evidence.get(s.name);
  console.log(`${ev ? '✅' : '❌'}  ${s.name.padEnd(9)} ${s.url}  ${ev ?? '未就绪'}`);
}
if (failed.length) {
  console.log(
    `\n未就绪：${failed.map((s) => s.name).join(', ')}（admin-api 的 nest 编译约需 15~25s；仍失败时查服务日志与端口占用）`,
  );
  process.exit(1);
}
process.exit(0);
