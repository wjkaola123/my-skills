/**
 * test-chat：机器人语音链路全流程自动化（Playwright 驱动 web 面板）
 *
 * 单命令完成：登录 → 机器人链路 → （缺设备时）新建设备并保存 Token →
 * 链路测试页连接 WS → 轮询 prompt.txt 等待用户文本 → 发送 → 帧级流式监控。
 *
 * 用法：
 *   node run.cjs [--timeout 180]   # 全流程，收到 tts stop 或超时后保持浏览器开启
 *   node run.cjs --new-device      # 删除固定设备并重建（设备记录坏了才用）
 *
 * 设备策略：固定复用「Playwright 自动化设备」，每回合 reset-token 换新 token 连接
 * （不在库里堆积 test-chat 设备）。设备不存在时首次走 UI 新建。
 *
 * 文件（都在 os.tmpdir()/dbca-test-flow/ 下，避免污染仓库）：
 *   device.json   固定设备记录（deviceId / uuid / 最新 token，仅供排查参考）
 *   prompt.txt    Claude 问完用户后写入要发送的文本；脚本读后即删
 *   ws-trace.jsonl 每帧完整 JSON 追加记录，供事后深挖调用链
 *   profile/      Chromium 持久 profile（localStorage 登录态复用，免重复登录）
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
// playwright 从 agent 仓 node_modules 解析（工作区根没有 node_modules，裸 require 会失败）：
// scripts/ → test-chat/ → skills/ → .zcode/ → 工作区根，再拼 agent；兜底裸解析（agent 仓内部署形态）
const AGENT_ROOT = path.resolve(__dirname, '..', '..', '..', '..', 'agent');
let chromium;
try {
  ({ chromium } = require(path.join(AGENT_ROOT, 'node_modules', 'playwright')));
} catch {
  ({ chromium } = require('playwright'));
}

const BASE = path.join(os.tmpdir(), 'dbca-test-flow');
const DEVICE_FILE = path.join(BASE, 'device.json');
const PROMPT_FILE = path.join(BASE, 'prompt.txt');
const TRACE_FILE = path.join(BASE, 'ws-trace.jsonl');
const PROFILE_DIR = path.join(BASE, 'profile');

const WEB_URL = process.env.TEST_FLOW_WEB_URL || 'http://localhost:5173';
const WEB_USER = process.env.TEST_FLOW_USER || 'admin';
const WEB_PASS = process.env.TEST_FLOW_PASS || 'admin123';

/** 固定复用的设备名：不每次新建，靠 reset-token 换新凭证 */
const FIXED_DEVICE_NAME = 'Playwright 自动化设备';

const args = process.argv.slice(2);
const timeoutIdx = args.indexOf('--timeout');
const TURN_TIMEOUT_MS = (timeoutIdx >= 0 ? Number(args[timeoutIdx + 1]) : 0) * 1000 || 180000;
const FORCE_NEW_DEVICE = args.includes('--new-device');

fs.mkdirSync(BASE, { recursive: true });

const now = () => new Date().toISOString();
const clock = () => {
  const d = new Date();
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
};
const log = (msg) => console.log(`[${clock()}] ${msg}`);

/** 事件流水里一行简报（对齐 web 端 renderBriefServerMsg 的口径，方便对照 UI） */
function brief(dir, obj) {
  if (!obj || typeof obj !== 'object') return `${dir} ${String(obj).slice(0, 80)}`;
  const t = obj.type ?? 'json';
  switch (t) {
    case 'hello':
      return dir === 'recv'
        ? `hello session=${obj.session_id ?? '?'}`
        : `hello v${obj.version ?? '?'} (audio=${obj.audio_params?.format ?? '?'})`;
    case 'listen':
      return obj.state === 'detect'
        ? `listen.detect "${String(obj.text ?? '').slice(0, 40)}"`
        : `listen.${obj.state}(${obj.mode ?? ''})`;
    case 'stt':
      return `stt "${String(obj.text ?? '').slice(0, 60)}"`;
    case 'llm': {
      const parts = [`llm emotion=${obj.emotion ?? '?'}`];
      if (obj.agent?.name) parts.push(`agent="${obj.agent.name}"`);
      if (obj.group?.name) parts.push(`group="${obj.group.name}"`);
      if (obj.refs?.length) parts.push(`refs=${obj.refs.length}`);
      if (obj.tools?.length) parts.push(`tools=${obj.tools.length}`);
      if (obj.model) parts.push(`model=${obj.model}`);
      return parts.join(' ');
    }
    case 'tool':
      return `tool[r${obj.round ?? '?'}] ${obj.ok === false ? '✗' : '✓'} ${obj.name ?? '?'} ${obj.elapsed_ms ?? obj.elapsedMs ?? ''}ms`;
    case 'tts': {
      let txt = '';
      if (obj.state === 'sentence_start' && typeof obj.text === 'string') {
        txt = obj.text.length > 60 ? ` "${obj.text.slice(0, 60)}…"` : ` "${obj.text}"`;
      }
      return `tts ${obj.state ?? '?'}${txt}`;
    }
    case 'alert':
      return `alert ${String(obj.message ?? '').slice(0, 120)}`;
    case 'session_settings':
      return 'session_settings';
    case 'abort':
      return 'abort';
    case 'goodbye':
      return 'goodbye';
    default:
      return `${t} ${JSON.stringify(obj).slice(0, 80)}`;
  }
}

const traceStream = fs.createWriteStream(TRACE_FILE, { flags: 'a' });
function trace(rec) {
  traceStream.write(JSON.stringify({ ts: now(), ...rec }) + '\n');
}

let stats = { jsonIn: 0, jsonOut: 0, binIn: 0, binInBytes: 0, binOut: 0, binOutBytes: 0, lastFrameAt: 0 };

function hookWs(page) {
  page.on('websocket', (ws) => {
    if (!ws.url().includes('/xiaozhi/v1/')) return;
    log(`WS_OPEN ${ws.url().replace(/token=[^&]+/, 'token=***')}`);
    trace({ ev: 'ws_open', url: ws.url().replace(/token=[^&]+/, 'token=***') });
    ws.on('framereceived', (frame) => {
      const payload = frame.payload; // string | Buffer（属性，非方法）
      stats.lastFrameAt = Date.now();
      if (typeof payload === 'string') {
        let obj = null;
        try { obj = JSON.parse(payload); } catch { /* non-JSON text */ }
        stats.jsonIn++;
        log(`← ${brief('recv', obj ?? payload)}`);
        trace({ dir: 'recv', data: obj ?? payload });
        page.emit('test-flow:frame', obj);
      } else {
        stats.binIn++; stats.binInBytes += payload.length;
        trace({ dir: 'recv', bin: payload.length });
      }
    });
    ws.on('framesent', (frame) => {
      const payload = frame.payload;
      stats.lastFrameAt = Date.now();
      if (typeof payload === 'string') {
        let obj = null;
        try { obj = JSON.parse(payload); } catch { /* ignore */ }
        stats.jsonOut++;
        log(`→ ${brief('send', obj ?? payload)}`);
        trace({ dir: 'send', data: obj ?? payload });
      } else {
        stats.binOut++; stats.binOutBytes += payload.length;
        trace({ dir: 'send', bin: payload.length });
      }
    });
    ws.on('close', () => {
      log('WS_CLOSE');
      trace({ ev: 'ws_close' });
      page.emit('test-flow:frame', { type: '__ws_closed__' });
    });
  });
}

/** 等到收到满足条件的帧（page.emit('test-flow:frame')） */
function waitFrame(page, predicate, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      page.off('test-flow:frame', onFrame);
      reject(new Error(`等待帧超时 ${timeoutMs}ms`));
    }, timeoutMs);
    function onFrame(obj) {
      if (predicate(obj)) {
        clearTimeout(timer);
        page.off('test-flow:frame', onFrame);
        resolve(obj);
      }
    }
    page.on('test-flow:frame', onFrame);
  });
}

/**
 * 在登录后的页面上下文里调 gateway 管理 API（/gateway-api 经 Vite 代理 → :3000/api）。
 * 鉴权沿用 web 自己的口径：Authorization: Bearer localStorage.dbca_token。
 */
async function gwApi(page, method, path, body) {
  return page.evaluate(async ({ method, path, body }) => {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = localStorage.getItem('dbca_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`/gateway-api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const json = await res.json().catch(() => null);
    if (!res.ok) {
      throw new Error(`${method} ${path} -> ${res.status} ${JSON.stringify(json)?.slice(0, 200)}`);
    }
    return json;
  }, { method, path, body });
}

async function main() {
  log(`RUN_START timeout=${TURN_TIMEOUT_MS / 1000}s new-device=${FORCE_NEW_DEVICE}`);

  let context;
  try {
    context = await chromium.launchPersistentContext(PROFILE_DIR, {
      headless: false,
      viewport: { width: 1600, height: 950 },
    });
  } catch (e) {
    if (/ProcessSingleton|Failed to launch/i.test(String(e))) {
      console.error(
        'ERROR_PROFILE_LOCKED: 上一个 test-chat 浏览器还占着 profile。\n' +
        '先 TaskStop 旧的 test-chat 后台任务（或手动关闭那个浏览器窗口）再重跑。',
      );
      process.exit(2);
    }
    throw e;
  }
  const page = context.pages()[0] || (await context.newPage());
  hookWs(page);

  // ---- 登录（profile 有登录态则跳过）----
  await page.goto(WEB_URL, { waitUntil: 'domcontentloaded', timeout: 20000 });
  const needLogin = await page
    .locator('input[type="password"]')
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  if (needLogin) {
    await page.fill('input[type="text"]', WEB_USER);
    await page.fill('input[type="password"]', WEB_PASS);
    await page.click('button[type="submit"]');
    await page.waitForSelector('input[type="password"]', { state: 'detached', timeout: 15000 });
    log('LOGIN_OK');
  } else {
    log('LOGIN_SKIP（profile 已有会话）');
  }

  await page.click('a[href="/robot-link"]');
  await page.waitForSelector('button:has-text("新建设备")', { timeout: 15000 });

  // ---- 设备：固定复用「Playwright 自动化设备」，每回合 reset-token 换新凭证 ----
  let fixed = (await gwApi(page, 'GET', '/robots'))
    .find((r) => r.name === FIXED_DEVICE_NAME);
  if (FORCE_NEW_DEVICE && fixed) {
    await gwApi(page, 'DELETE', `/robots/${fixed.id}`);
    fixed = null;
    log('DEVICE_DELETED（--new-device：固定设备已删除，将重建）');
  }
  if (fixed && !fixed.enabled) {
    await gwApi(page, 'PATCH', `/robots/${fixed.id}`, { enabled: true });
    log('DEVICE_RE_ENABLED（设备曾被禁用，已自动恢复）');
  }

  let device = null;
  if (fixed) {
    const r = await gwApi(page, 'POST', `/robots/${fixed.id}/reset-token`, {});
    if (!r?.token) throw new Error('reset-token 响应缺 token: ' + JSON.stringify(r).slice(0, 200));
    device = {
      deviceId: fixed.deviceId,
      uuid: fixed.id,
      name: fixed.name,
      token: r.token,
      createdAt: now(),
    };
    fs.writeFileSync(DEVICE_FILE, JSON.stringify(device, null, 2) + '\n', 'utf8');
    log(`DEVICE_RESET_TOKEN ${device.deviceId}（固定设备，旧 token 已作废）`);
  }
  if (!device) {
    // 首次引导：库里还没有固定设备，走 UI 新建一次（此后每回合都走 reset-token 复用）
    await page.click('button:has-text("新建设备")');
    await page.waitForSelector('input[placeholder="aa:bb:cc:dd:ee:ff"]', { timeout: 10000 });
    const mac = ['e2', ...crypto.randomBytes(5).toString('hex').match(/../g)].join(':');
    await page.fill('input[placeholder="aa:bb:cc:dd:ee:ff"]', mac);
    await page.fill('input[placeholder="例如：客厅的小智"]', FIXED_DEVICE_NAME);
    const sectionVal = await page.inputValue('div.fixed select >> nth=0');
    if (!sectionVal) throw new Error('没有可用板块（enabled sections 为空），先在 web 上建一个板块');
    const respPromise = page.waitForResponse(
      (r) => r.request().method() === 'POST' && /\/robots\/?$/.test(new URL(r.url()).pathname),
      { timeout: 20000 },
    );
    await page.click('button:has-text("创建并生成 Token")');
    const body = await respPromise.then((r) => r.json());
    if (!body?.token) throw new Error('POST /robots 响应缺 token: ' + JSON.stringify(body).slice(0, 200));
    device = {
      deviceId: body.device?.deviceId ?? mac.replace(/:/g, ''),
      uuid: body.device?.id ?? null,
      name: FIXED_DEVICE_NAME,
      token: body.token,
      createdAt: now(),
    };
    fs.writeFileSync(DEVICE_FILE, JSON.stringify(device, null, 2) + '\n', 'utf8');
    log(`DEVICE_CREATED ${device.deviceId}「${FIXED_DEVICE_NAME}」token 已存 ${DEVICE_FILE}`);
    await page.click('button:has-text("我已保存")').catch(() => {});
  }

  // ---- 链路测试页：选设备、填 token、连接 ----
  await page.click('button:has-text("链路测试")');
  await page.waitForSelector('text=连接设置', { timeout: 10000 });
  const deviceSel = page.locator('label:has-text("选择设备") select');
  await deviceSel.waitFor({ timeout: 10000 });
  const optVal = await deviceSel.locator('option', { hasText: device.deviceId }).getAttribute('value');
  if (!optVal) {
    throw new Error(
      `设备 ${device.deviceId} 不在下拉框里（可能被并发删除）。加 --new-device 重建固定设备。`,
    );
  }
  await deviceSel.selectOption(optVal);
  await page.fill('input[placeholder="粘贴 plainToken"]', device.token);
  log(`DEVICE_SELECTED ${device.deviceId}`);

  await page.click('button:has-text("连接")');
  try {
    const hello = await waitFrame(page, (o) => o?.type === 'hello' && o?.session_id, 20000);
    log(`SESSION_ESTABLISHED ${hello.session_id}`);
  } catch {
    throw new Error(
      '连接失败（20s 内未收到 hello.session_id）。token 是本回合刚重置的，' +
      '一般不是凭证问题：gateway 未启动 → 先跑 startup skill；设备状态异常 → --new-device 重建。',
    );
  }

  // ---- 等用户文本：Claude 问完用户后写 prompt.txt ----
  log('WAITING_FOR_PROMPT（写入 ' + PROMPT_FILE + ' 后自动发送）');
  const promptStart = Date.now();
  let text = null;
  while (Date.now() - promptStart < 600000) {
    if (fs.existsSync(PROMPT_FILE)) {
      text = fs.readFileSync(PROMPT_FILE, 'utf8').trim();
      if (text) { fs.unlinkSync(PROMPT_FILE); break; }
    }
    await page.waitForTimeout(500);
  }
  if (!text) throw new Error('10 分钟内没等到 prompt.txt，放弃本次回合');
  log(`SENDING "${text.slice(0, 60)}${text.length > 60 ? '…' : ''}"`);

  // ---- 发送 + 流式监控，直到 tts stop / 超时 ----
  const ta = page.locator('textarea');
  await ta.fill(text);
  await page.click('button:has-text("发送")');
  trace({ ev: 'turn_start', text, at: now() });

  const turnStart = Date.now();
  // 终止条件：turn_metric（gateway 的权威回合结束信号）后静默 2s；
  // 兜底：tts stop 后静默 5s（老版本 gateway 或 TTS 失败路径会提前 stop）；
  // 再兜底：总超时。tts stop 单独不可靠——TTS 401 时 stop 先来，LLM/tool 还在跑。
  let sawTtsStop = false;
  let metric = null;
  page.on('test-flow:frame', (o) => {
    if (o?.type === 'tts' && o?.state === 'stop') sawTtsStop = true;
    if (o?.type === 'turn_metric') metric = o;
  });
  stats.lastFrameAt = Date.now();
  while (true) {
    await page.waitForTimeout(300);
    const idle = Date.now() - stats.lastFrameAt;
    if (metric && idle >= 2000) break;
    if (sawTtsStop && idle >= 5000) break;
    if (Date.now() - turnStart >= TURN_TIMEOUT_MS) break;
  }
  const durMs = Date.now() - turnStart;

  const errors = [];
  for (const line of fs.readFileSync(TRACE_FILE, 'utf8').split('\n').slice(-200)) {
    if (!line) continue;
    try {
      const rec = JSON.parse(line);
      if (rec?.data?.type === 'alert') errors.push(rec.data.message);
    } catch { /* ignore */ }
  }

  console.log('=== SUMMARY ===');
  console.log(`turn_ms=${durMs} end_signal=${metric ? 'turn_metric' : sawTtsStop ? 'tts_stop+quiet' : 'timeout'}`);
  if (metric) {
    console.log(
      `turn_metric reply_chars=${metric.reply_chars ?? '?'} ` +
      `llmTtftMs=${metric.phases?.llmTtftMs ?? '?'} llmTotalMs=${metric.phases?.llmTotalMs ?? '?'} ` +
      `replyCompleteMs=${metric.phases?.replyCompleteMs ?? '?'}`,
    );
  }
  console.log(
    `json_in=${stats.jsonIn} json_out=${stats.jsonOut} ` +
    `bin_in=${stats.binIn}frames/${stats.binInBytes}B bin_out=${stats.binOut}frames/${stats.binOutBytes}B`,
  );
  console.log(`alerts=${errors.length ? JSON.stringify(errors) : 'none'}`);
  console.log(`TRACE=${TRACE_FILE}`);
  console.log(`DEVICE=${DEVICE_FILE}`);
  trace({ ev: 'turn_end', durMs, metric: metric ?? null, stats });

  // 保持浏览器开着：用户要看 UI 上的事件流水 / 数字孪生时直接看
  log('BROWSER_KEPT_OPEN（关闭浏览器窗口或 TaskStop 本任务即结束）');
  await new Promise((resolve) => context.on('close', resolve));
  console.log('BROWSER_CLOSED');
  process.exit(0);
}

main().catch((e) => {
  console.error('FATAL: ' + (e?.message ?? e));
  process.exit(1);
});
