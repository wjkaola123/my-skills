#!/usr/bin/env node
/**
 * 故事导入对账（story-import skill Phase 4 交付门）。
 *
 * 校验某本故事的 DB audio_file ↔ 磁盘文件 ↔ 容器可解析性三方一致：
 *   1. story_nodes 每行 audio_file 按 MEDIA_LIBRARY_DIR 解析后文件存在；
 *   2. 引擎同款 decodeOpusFrames 可解析且帧数 > 0；
 *   3. story-nodes/{sid}/ 下无对应不到 DB 的孤儿文件；
 *   4. --report <conversion-report.json> 给了则逐文件核对帧数（转码源报告）。
 *
 * exit 0 = 全部通过；exit 1 = 有缺口（缺文件/不可解析/孤儿/帧数不符）。
 *
 * 用法：
 *   node verify-story-audio.mjs --story 82
 *   node verify-story-audio.mjs --title "Magical Land Chapter One" \
 *        --report D:/Projects/audio/opusframes/conversion-report.json
 *
 * ⚠ 本脚本在 .zcode/skills 下，npm 依赖必须显式指到 agent 仓 node_modules
 *   （先例：test-chat skill 的 playwright 注记），勿改成裸 import。
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// scripts/ → story-import/ → skills/ → .zcode/ → 工作区根（/home/wj/project），再拼 agent 仓
const AGENT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'agent');

// ── 参数 ──
const a = {};
for (let i = 2; i < process.argv.length; i++) {
  const k = process.argv[i];
  if (k.startsWith('--')) a[k.slice(2).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = process.argv[++i];
}
if (!a.story && !a.title) {
  console.error('用法：node verify-story-audio.mjs --story <sid> | --title <title> [--report <conversion-report.json>] [--env <path>]');
  process.exit(1);
}

// ── .env：DATABASE_URL + MEDIA_LIBRARY_DIR ──
const envPath = a.env ?? join(AGENT, '.env');
const envText = readFileSync(envPath, 'utf8');
const env = (key) => envText.match(new RegExp(`^${key}=(.+)$`, 'm'))?.[1]?.trim();
const dsn = a.db || process.env.DATABASE_URL || env('DATABASE_URL');
if (!dsn) { console.error('✗ 缺 DATABASE_URL'); process.exit(1); }
// 引擎同款：MEDIA_LIBRARY_DIR || './data/media-library'（按 gateway cwd 解析）
const mediaDir = process.env.MEDIA_LIBRARY_DIR
  ?? env('MEDIA_LIBRARY_DIR')
  ?? join(AGENT, 'packages/gateway/data/media-library');

// ── 依赖（显式指 agent 仓）──
const pg = (await import(pathToFileURL(join(AGENT, 'node_modules/pg/lib/index.js')).href)).default;
const { decodeOpusFrames } = await import(
  pathToFileURL(join(AGENT, 'packages/gateway/dist/media-library/opusframes.js')).href
);

const report = a.report && existsSync(a.report)
  ? new Map(JSON.parse(readFileSync(a.report, 'utf8')).files.map((f) => [f.id, f.frames]))
  : null;

const { Client } = pg;
const c = new Client({ connectionString: dsn });
await c.connect();
const where = a.story ? 'id = $1' : 'title = $1';
const ident = a.story ?? a.title;
const s = await c.query(`SELECT id, title, status FROM stories WHERE ${where}`, [ident]);
if (s.rowCount === 0) { console.error(`✗ 故事不存在：${ident}`); process.exit(1); }
const sid = s.rows[0].id;
const { rows } = await c.query('SELECT id, audio_file FROM story_nodes WHERE story_id = $1 ORDER BY id', [sid]);
await c.end();

console.log(`故事 id=${sid} "${s.rows[0].title}" (${s.rows[0].status})｜MEDIA_LIBRARY_DIR=${mediaDir}`);
let bad = 0;
const dir = join(mediaDir, `story-nodes/${sid}`);
const disk = new Set(existsSync(dir) ? readdirSync(dir) : []);

for (const r of rows) {
  const full = join(mediaDir, r.audio_file);
  const fname = r.audio_file.split('/').pop();
  disk.delete(fname);
  if (!existsSync(full)) { console.log(`✗ 节点${r.id} 缺文件 ${full}`); bad++; continue; }
  let frames;
  try {
    frames = decodeOpusFrames(readFileSync(full)).frames.length;
  } catch (e) {
    console.log(`✗ 节点${r.id} 容器不可解析 ${fname}：${e.message}`); bad++; continue;
  }
  const localId = fname.replace('.opusframes', '');
  const expect = report?.get(localId);
  const rep = expect === undefined ? '' : (frames === expect ? `｜报告一致(${expect}帧)` : `｜报告不符(${frames}≠${expect})`);
  if (expect !== undefined && frames !== expect) bad++;
  console.log(`✓ 节点${r.id} ${fname.padEnd(32)} ${String(frames).padStart(5)}帧 ${(frames * 60 / 1000).toFixed(1)}s${rep}`);
}
if (disk.size > 0) { console.log(`✗ 孤儿文件（DB 无引用）：${[...disk].join(', ')}`); bad += disk.size; }

console.log(bad === 0 ? `\n通过：${rows.length} 节点音频全部就位且可解析` : `\n未通过：${bad} 处缺口`);
process.exit(bad === 0 ? 0 : 1);
