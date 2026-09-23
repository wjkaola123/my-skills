#!/usr/bin/env node
/**
 * stop-all 技能的停止脚本：按 .env 解析三个服务端口，找出 LISTENING
 * 进程，杀进程树，复查端口全部释放。
 * 全部端口空闲（含本来就没事可做）退出码 0；仍有占用退出码 1。
 *
 * 双平台：Windows 走 netstat -ano + taskkill /F /T；
 * Linux/WSL 走 ss -ltnp + SIGKILL——ss 直接给出持有 socket 的 node 进程，
 * 杀它即可（npm 外层进程随子进程退出收尾，无孤儿问题）。
 *
 * 用法：node /home/wj/project/.zcode/skills/stop-all/scripts/stop.mjs [--quiet]
 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const run = promisify(execFile);
const quiet = process.argv.includes('--quiet');
const IS_WIN = process.platform === 'win32';

// 与 startup/scripts/verify.mjs 同一套端口解析规则
// scripts/ → stop-all/ → skills/ → .zcode/ → 工作区根（/home/wj/project），再拼 agent 仓
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
const gwPort = Number(env.PORT) || 3000;
const adminPort =
  Number(env.ADMIN_API_PORT) ||
  (Number(env.PORT) && Number(env.PORT) !== 3000 ? Number(env.PORT) : 3001);
const webPort = 5173;

const targets = [
  { name: 'gateway', port: gwPort },
  { name: 'admin-api', port: adminPort },
  { name: 'web', port: webPort },
];

/** 端口 → pid[]（仅 LISTENING，去重，剔除 PID 0/4）。 */
async function listeningPids() {
  const map = new Map();
  const add = (port, pid) => {
    if (!port || !Number.isFinite(pid) || pid <= 0 || pid === 4) return;
    if (!map.has(port)) map.set(port, new Set());
    map.get(port).add(pid);
  };
  if (IS_WIN) {
    // TCP  本地地址           远程地址        状态           PID
    const { stdout } = await run('netstat', ['-ano']);
    for (const line of stdout.split(/\r?\n/)) {
      const cols = line.trim().split(/\s+/);
      if (cols.length < 5 || cols[3] !== 'LISTENING') continue;
      add(Number(cols[1].split(':').pop()), Number(cols[4]));
    }
  } else {
    // LISTEN 0 511 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=1234,fd=20))
    // pid= 信息需要进程属主或 root 权限；拿不到 pid 的监听行无法处理（见尾部提示）
    const { stdout } = await run('ss', ['-ltnp']);
    for (const line of stdout.split(/\n/)) {
      if (!line.includes('LISTEN')) continue;
      const cols = line.trim().split(/\s+/);
      const port = Number((cols[3] ?? '').split(':').pop());
      for (const m of line.matchAll(/pid=(\d+)/g)) add(port, Number(m[1]));
    }
  }
  return map;
}

/** 杀进程：Windows taskkill /F /T 杀整棵树（GBK 输出吞掉）；Linux 对持 socket 进程 SIGKILL。 */
async function killTree(pid) {
  try {
    if (IS_WIN) {
      await run('taskkill', ['/F', '/T', '/PID', String(pid)], { windowsHide: true });
    } else {
      process.kill(pid, 'SIGKILL');
    }
    return true;
  } catch {
    return false;
  }
}

const before = await listeningPids();
let killedAny = false;

for (const t of targets) {
  const pids = [...(before.get(t.port) ?? [])];
  if (pids.length === 0) {
    if (!quiet) console.log(`⏭️  ${t.name.padEnd(9)} :${t.port}  本就没在监听`);
    continue;
  }
  const results = await Promise.all(pids.map((p) => killTree(p)));
  killedAny = true;
  const ok = results.filter(Boolean).length;
  if (!quiet)
    console.log(`🛑 ${t.name.padEnd(9)} :${t.port}  杀掉 PID ${pids.join(', ')}${ok < pids.length ? `（${pids.length - ok} 个 kill 失败）` : ''}`);
}

// 复查：给内核一点时间收掉 socket，最多等 ~5s
let after;
for (let i = 0; i < 5; i++) {
  await new Promise((r) => setTimeout(r, 1000));
  after = await listeningPids();
  if (targets.every((t) => !(after.get(t.port)?.size > 0))) break;
}

const stuck = targets.filter((t) => after.get(t.port)?.size > 0);
if (killedAny || stuck.length) console.log('');
for (const t of targets) {
  const pids = [...(after.get(t.port) ?? [])];
  if (pids.length === 0) {
    if (killedAny || !quiet) console.log(`✅  ${t.name.padEnd(9)} :${t.port}  端口已释放`);
  } else {
    console.log(`❌  ${t.name.padEnd(9)} :${t.port}  仍被 PID ${pids.join(', ')} 占用`);
  }
}

if (stuck.length) {
  const hint = IS_WIN
    ? 'netstat -ano | grep LISTENING | grep :<port>'
    : 'ss -ltnp | grep :<port>（看不到 pid= 时用 sudo 重跑）';
  console.log(`\n未释放：${stuck.map((t) => t.name).join(', ')}。用 ${hint} 人工确认后再决策（可能是其他程序撞了端口，别盲杀）。`);
  process.exit(1);
}
process.exit(0);
