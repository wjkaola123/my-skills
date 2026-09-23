// encode_frames.js — wav(16k/16bit/mono) → .frames(4B 小端长度前缀 opus 帧序列)
// 依赖 agent 仓 node_modules 的 opusscript(wasm); 用法: node encode_frames.js <in.wav> <out.frames>
const fs = require('node:fs');
const path = require('node:path');

// scripts/ → story-flow-test/ → skills/ → .zcode/ → 工作区根，再拼 agent 仓（可用 AGENT_DIR 覆盖）
const AGENT_DIR = process.env.AGENT_DIR || path.resolve(__dirname, '..', '..', '..', '..', 'agent');
const opusscript = require(path.join(AGENT_DIR, 'node_modules/opusscript'));
const FRAME_MS = 60, RATE = 16000, SAMPLES = RATE * FRAME_MS / 1000; // 960

function wavToPcm(file) {
  const buf = fs.readFileSync(file);
  let off = 12;
  while (off + 8 <= buf.length) {
    const id = buf.toString('latin1', off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === 'data') return buf.subarray(off + 8, off + 8 + size);
    off += 8 + size + (size % 2);
  }
  throw new Error('no data chunk: ' + file);
}

const [, , inWav, outFrames] = process.argv;
if (!inWav || !outFrames) {
  console.error('usage: node encode_frames.js <in.wav> <out.frames>');
  process.exit(2);
}
const pcm = wavToPcm(inWav);
const enc = new opusscript(RATE, 1, opusscript.APPLICATION_VOIP);
const body = [];
let count = 0;
for (let i = 0; i < pcm.length; i += SAMPLES * 2) {
  let chunk = pcm.subarray(i, i + SAMPLES * 2);
  if (chunk.length < SAMPLES * 2) {
    chunk = Buffer.concat([chunk, Buffer.alloc(SAMPLES * 2 - chunk.length)]);
  }
  const c = enc.encode(chunk, SAMPLES);
  const len = Buffer.alloc(4);
  len.writeUInt32LE(c.length, 0);
  body.push(len, c);
  count++;
}
enc.delete();
fs.writeFileSync(outFrames, Buffer.concat(body));
console.log(`encoded ${count} frames (${(count * FRAME_MS / 1000).toFixed(1)}s) -> ${outFrames}`);
