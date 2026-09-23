"""story_flow.py — AG-20 故事引擎全流程自动化测试（图驱动·随机决策边）

模拟 W810 常开麦设备（持续音频流+VAD），从 SELECTING 进入到 PLAYING 决策
问答循环、走到 ENDING 终点自动退出，全程黑盒：真实 paraformer ASR + 真实
LLM 召回门/语义路由 + 真实 Redis/PG/音频文件。

驱动方式（图驱动, 不解析提问文本）:
  1. 清场 → 说 "Tell me a story"（未进故事则换措辞重试, 绕开 LRU 缓存）
  2. 循环: 读 Redis story:session current_node → 查 PG 图出边
     - 出边 ≥2: 决策点 → 随机选一条边 → 动态合成其 semantic_description
       语音（Zira TTS + opusscript 编码, 同边缓存）→ 发送 → 等媒体段+下一提问
     - 出边 =0: 终点 → 验证结束语已播、exit 清理（active 删/hash+PG 留存）
     - 出边 =1: 异常（段链应自动连播过单出边节点）→ 记 FAIL
  3. 每步断言: W810 start/end 契约 / 媒体段帧数==ΣframeCount /
     current_node 锚定到预期 run 末节点 / 路由 miss 时 "Let's " 变体重试

前置（缺一不可, 详见 SKILL.md）:
  - gateway 3000 运行, .env 含 STORY_RECALL_GATE_MODE=active
  - PG story 表有 published 故事 + MEDIA_LIBRARY_DIR/story-nodes/ 音频在盘
  - 本机: python(requests/websockets/redis/psycopg2) + node(agent 仓 opusscript)
    + PowerShell System.Speech(Zira en-US)
  - aip_client 包在 D:\\Tools\\aip_client（协议客户端/编码工具）

用法:
  python story_flow.py                     # 随机路径全流程
  python story_flow.py --seed 42           # 可复现的随机路径
  python story_flow.py --story 82 --device mock-w810-002 --secret mock-secret-654321
  python story_flow.py --max-steps 30      # 回环图兜底（默认 25）
  python story_flow.py --enter-keep        # 不清场（续玩/调试）
结果: 控制台矩阵 + story_flow_result.json（轨迹/窗口流/断言, 供 CI 消费）
"""
import argparse
import json
import random
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent.resolve()
IS_WIN = sys.platform == 'win32'
if IS_WIN:
    AIP_CLIENT_DIR = Path('D:/Tools/aip_client')   # aip_client 包目录（协议客户端）
    AGENT_DIR = Path('D:/Projects/agent')
else:  # WSL: aip_client 与 TTS 缓存走 /mnt/d（powershell.exe 互操作可见），仓/音频在本地
    AIP_CLIENT_DIR = Path('/mnt/d/Tools/aip_client')
    AGENT_DIR = Path('/home/wj/project/agent')
sys.path.insert(0, str(AIP_CLIENT_DIR.parent))  # 包的父目录入 path

import redis as redis_lib
import psycopg2
import psycopg2.extras

from aip_client import AipMockClient
from aip_client.codec import encode_aip_frame, decode_aip_frame
from aip_client.constants import AIP_BIN_AUDIO_UPLINK, AIP_BIN_AUDIO_DOWNLINK

BASE_URL = 'http://127.0.0.1:3000'
MEDIA_DIR = Path('D:/story/audio') if IS_WIN else Path('/home/wj/story-audio')
CACHE_DIR = (SKILL_DIR / '.cache') if IS_WIN else (AIP_CLIENT_DIR / '.cache_story')
STORY_ID = '83' if not IS_WIN else '82'   # WSL 本地导入的是 83/84/85（83=Magical Land）
DEVICE_ID = 'mock-w810-001'
DEVICE_SECRET = 'mock-secret-123456'
USER_ID_FALLBACK = f'device:{DEVICE_ID}'
# AG-57（互动故事门）后粗筛只认互动层/续玩/点名宽口径——泛化措辞
# （"Tell me a story"）刻意放行闲聊现编，进不了故事门。变体绕 LRU 缓存。
ENTER_PHRASES = ['I want an interactive story', 'Play an interactive story please',
                 'Interactive story time']

RESULTS = []
TRAJECTORY = []   # [{'from':node,'to':node,'edge':label,'by':'random|enter'}]


def check(name, ok, detail=''):
    RESULTS.append({'name': name, 'ok': ok, 'detail': detail})
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f' — {detail}' if detail else ''),
          flush=True)
    return ok


# ─── 基础设施（Redis 双栈 / PG / uid 发现） ─────────────────────

def _redis_try(host):
    try:
        r = redis_lib.Redis(host=host, port=6379, decode_responses=True, protocol=2)
        r.ping()
        return r
    except Exception:
        return None


def redis_all():
    """Windows 双栈双实例: gateway getRedis 连 localhost(=::1)。全部返回供清场。"""
    rs = [r for r in (_redis_try(h) for h in ('::1', '127.0.0.1')) if r]
    if not rs:
        raise RuntimeError('Redis 不可达 (::1 / 127.0.0.1 均失败)')
    return rs


def discover_uid(r, story_id):
    """从 story:active:{uid} 提取; exit 后 active 已删, 回退扫 session key。"""
    for k in r.keys('story:active:*'):
        return k[len('story:active:'):]
    for k in r.keys(f'story:session:*:{story_id}'):
        return k.split(':')[2]
    return USER_ID_FALLBACK


def pg():
    return psycopg2.connect(
        'postgresql://agent:dev_password@localhost:5432/agent_cluster')


def redis_state(r, uid, story_id):
    return (r.get(f'story:active:{uid}'),
            r.hgetall(f'story:session:{uid}:{story_id}'))


def clear_state(story_id):
    n = 0
    for r in redis_all():
        keys = list(r.keys('story:*'))
        if keys:
            r.delete(*keys)
            n += len(keys)
    with pg() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM user_story_progress WHERE story_id=%s', (story_id,))
        conn.commit()
    print(f'[清场] redis story keys ×{n}; PG progress(全部 user) 删除', flush=True)


# ─── 故事图（PG 拉取 + OPFR 帧数 + 段链预排镜像） ────────────────

class StoryGraph:
    def __init__(self, story_id):
        with pg() as conn, conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT title FROM stories WHERE id=%s', (story_id,))
            row = cur.fetchone()
            self.title = row['title'] if row else None
            cur.execute('SELECT id, content, audio_file, is_start, is_end '
                        'FROM story_nodes WHERE story_id=%s ORDER BY id',
                        (story_id,))
            self.nodes = {str(r['id']): {
                'content': r['content'], 'audio': r['audio_file'],
                'is_start': r['is_start'], 'is_end': r['is_end'],
                'frames': opfr_frames(MEDIA_DIR / r['audio_file']),
            } for r in cur.fetchall()}
            cur.execute('SELECT source_node_id, target_node_id, semantic_description '
                        'FROM story_edges WHERE story_id=%s ORDER BY id', (story_id,))
            self.edges = [(str(r['source_node_id']), str(r['target_node_id']),
                           r['semantic_description'] or '') for r in cur.fetchall()]
        self.start = next((k for k, v in self.nodes.items() if v['is_start']), None)
        self.ends = [k for k, v in self.nodes.items() if v['is_end']]

    def out_edges(self, node_id):
        return [(t, label) for s, t, label in self.edges if s == node_id]

    def plan_run(self, start):
        """镜像 SegmentPlanner.planChain: 沿唯一出边收集, 决策点/终点收口。
        防纯单边环（visited）——与网关构建期防环同型。"""
        run, cur, visited = [start], start, {start}
        while True:
            edges = self.out_edges(cur)
            if len(edges) != 1:
                return run
            nxt = edges[0][0]
            if nxt in visited:
                return run  # cycle-guard: run 到环入口前收口
            run.append(nxt)
            visited.add(nxt)
            cur = nxt


def opfr_frames(path):
    """OPFR [16B 大端头] + [2BE len][帧]*: 返回 frameCount（时长=×60ms）。"""
    buf = Path(path).read_bytes()
    if buf[:4] != b'OPFR':
        raise ValueError(f'bad OPFR magic: {path}')
    return int.from_bytes(buf[12:16], 'big')


def load_frames(path):
    data = Path(path).read_bytes()
    frames, off = [], 0
    while off + 4 <= len(data):
        (n,) = struct.unpack_from('<I', data, off)
        off += 4
        frames.append(data[off:off + n])
        off += n
    return frames


# ─── 动态语音合成（Zira TTS → opusscript 编码, 按短语缓存） ────────

PS_SHELL_TMPL = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Zira Desktop')
$s.Rate = -1
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{wav}', $fmt)
$s.Speak('{text}')
$s.SetOutputToNull()
"""


def ensure_silence_frames():
    """静音资产自生成（20 帧×60ms）：优先复用 aip_client 的 _silence.frames，
    缺失则用 agent 仓 opusscript 现场编码——skill 不硬依赖外部资产文件。"""
    src = AIP_CLIENT_DIR / '_silence.frames'
    if src.exists():
        return src
    out = CACHE_DIR / '_silence.frames'
    if out.exists():
        return out
    node_code = (
        "const opusscript=require(process.env.AGENT_DIR+'/node_modules/opusscript');"
        "const enc=new opusscript(16000,1,opusscript.APPLICATION_VOIP);"
        "const sil=Buffer.alloc(960*2);const body=[];"
        "for(let i=0;i<20;i++){const c=enc.encode(sil,960);"
        "const l=Buffer.alloc(4);l.writeUInt32LE(c.length,0);body.push(l,c);}"
        "require('fs').writeFileSync(process.argv[1],Buffer.concat(body));")
    subprocess.run(['node', '-e', node_code, str(out)], check=True,
                   capture_output=True, env={**__import__('os').environ,
                                             'AGENT_DIR': str(AGENT_DIR)})
    return out


def _win_path(p):
    """WSL '/mnt/d/a/b' → 'D:\\a\\b'（powershell.exe 互操作需 Windows 视图路径）。"""
    m = re.match(r'^/mnt/([a-z])(/.*)$', str(p))
    return (m.group(1).upper() + ':' + m.group(2).replace('/', '\\')) if m else str(p)


def synth_frames(text):
    """英文短语 → .frames（缓存; Zira 合成 + agent 仓 opusscript 编码）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')[:60] or 'phrase'
    out = CACHE_DIR / f'{key}.frames'
    if out.exists():
        return out
    wav = CACHE_DIR / f'{key}.wav'
    script = PS_SHELL_TMPL.format(wav=_win_path(wav).replace("'", "''"),
                                  text=text.replace("'", "''"))
    ps_exe = 'powershell' if IS_WIN else 'powershell.exe'
    ps_kw = {} if IS_WIN else {'cwd': str(AIP_CLIENT_DIR), 'timeout': 120}
    subprocess.run([ps_exe, '-NoProfile', '-Command', script],
                   check=True, capture_output=True, **ps_kw)
    # cwd 不指向 agent 仓（require 走绝对路径）——避免任何文件事件触发 tsx watch
    subprocess.run(['node', str(SKILL_DIR / 'scripts' / 'encode_frames.js'),
                    str(wav), str(out)],
                   check=True, capture_output=True,
                   env={**__import__('os').environ, 'AGENT_DIR': str(AGENT_DIR)})
    return out


# ─── 常开麦 + 事件流收集（经验沉淀: IdleTimeout/turnState 两坑的解） ──

class UplinkFeeder:
    """模拟 W810 常开麦: 空闲持续发静音帧（防 paraformer ~60s IdleTimeout 杀流
    + hub 惰性重建丢帧）; 说话窗口独占发送（60ms/帧实时节奏）。"""

    def __init__(self, client, silence_frames):
        self.client = client
        self.sil = silence_frames
        self.idx = 0
        self.speech_active = threading.Event()
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    dead = None  # ConnectionClosed 等: 连接已断, 记录原因

    def _send(self, payload):
        binary = encode_aip_frame(
            stream_id=0, kind=AIP_BIN_AUDIO_UPLINK, seq=self.client.tx_seq,
            ts_ms=int(time.time() * 1000), flags=0, payload=payload)
        with self.lock:
            self.client.tx_seq += 1
            try:
                self.client.ws.send(binary)
            except Exception as e:
                self.dead = str(e)
                self.stop_flag.set()

    def _loop(self):
        next_t = time.time()
        while not self.stop_flag.is_set():
            now = time.time()
            if now < next_t:
                time.sleep(min(0.02, next_t - now))
                continue
            if not self.speech_active.is_set():
                self._send(self.sil[self.idx % len(self.sil)])
                self.idx += 1
            next_t += 0.06

    def speak_frames(self, frames):
        self.speech_active.set()
        try:
            t0 = time.time()
            for i, fr in enumerate(frames):
                self._send(fr)
                delay = t0 + (i + 1) * 0.06 - time.time()
                if delay > 0:
                    time.sleep(delay)
        finally:
            self.speech_active.clear()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()


class FlowRecorder:
    """下行事件流按 voice.tts.start/end 切窗口, 在线校验 W810 契约。
    窗口分型: tts(有 agent.text)=CosyVoice 段; media(有帧无 agent.text)=预录段。"""

    def __init__(self, client):
        self.client = client
        self.windows = []
        self.violations = []
        self.asr_finals = []
        self.errors = []
        self.session_closed = None
        self._open = False

    def _win(self):
        if not self.windows or self.windows[-1]['closed']:
            self.windows.append({'kind': 'open', 'frames': 0, 'bytes': 0,
                                 'sentences': [], 'agent_text': None,
                                 'closed': False})
        return self.windows[-1]

    def _close_win(self):
        w = self._win()
        w['closed'] = True
        w['kind'] = ('tts' if w['agent_text'] is not None
                     else 'media' if (w['sentences'] or w['frames'] > 0)
                     else 'empty')

    def collect(self, min_ends, idle_gap=10.0, timeout=120.0):
        deadline = time.time() + timeout
        ends, last_msg = 0, time.time()
        while time.time() < deadline:
            try:
                raw = self.client.ws.recv(timeout=0.3)
            except Exception as e:
                if 'Closed' in type(e).__name__ or 'close' in str(e).lower():
                    self.session_closed = f'connection_lost: {e}'
                raw = None
            now = time.time()
            if raw is None:
                idle_needed = idle_gap if ends >= min_ends else idle_gap * 2
                if now - last_msg >= idle_needed or self.session_closed or self.errors:
                    break
                continue
            last_msg = now
            if isinstance(raw, bytes):
                f = decode_aip_frame(raw)
                if f and f['header']['kind'] == AIP_BIN_AUDIO_DOWNLINK:
                    if not self._open:
                        self.violations.append('audio frame outside tts window')
                    w = self._win()
                    w['frames'] += 1
                    w['bytes'] += f['header']['payload_len']
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t, p = msg.get('type', ''), msg.get('payload', {})
            if t == 'voice.tts.start':
                if self._open:
                    self.violations.append('tts.start while window open')
                self._open = True
                self._win()
            elif t == 'voice.tts.end':
                if not self._open:
                    self.violations.append('tts.end without open window')
                self._open = False
                self._close_win()
                ends += 1
            elif t == 'voice.tts.sentence':
                self._win()['sentences'].append(p.get('text', ''))
            elif t == 'agent.text':
                self._win()['agent_text'] = p.get('text', '')
            elif t == 'voice.asr.final':
                self.asr_finals.append(p.get('text', ''))
            elif t == 'error.report':
                self.errors.append({'code': p.get('code'), 'message': p.get('message')})
            elif t == 'session.closed':
                self.session_closed = p.get('reason')
        return self

    def media_windows(self):
        return [w for w in self.windows if w['kind'] == 'media']

    def tts_windows(self):
        return [w for w in self.windows if w['kind'] == 'tts']

    def dump(self):
        for i, w in enumerate(self.windows):
            head = f"    #{i} {w['kind']:5s} frames={w['frames']:4d} bytes={w['bytes']}"
            if w['agent_text'] is not None:
                head += f" text=\"{(w['agent_text'] or '')[:70]}\""
            if w['sentences']:
                head += f" sentences×{len(w['sentences'])}"
            print(head, flush=True)
        if self.asr_finals:
            print(f'    asr_finals={self.asr_finals}', flush=True)
        if self.errors:
            print(f'    errors={self.errors}', flush=True)


def speak_and_collect(client, feeder, recorder, frames_file, min_ends, timeout):
    frames = load_frames(frames_file) + feeder.sil[:20]
    th = threading.Thread(target=feeder.speak_frames, args=(frames,), daemon=True)
    th.start()
    recorder.collect(min_ends=min_ends, idle_gap=10.0, timeout=timeout)
    th.join(timeout=10)


# ─── 主流程 ───────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--story', default=STORY_ID)
    ap.add_argument('--device', default=DEVICE_ID)
    ap.add_argument('--secret', default=DEVICE_SECRET)
    ap.add_argument('--seed', type=int, default=None, help='随机种子(可复现路径)')
    ap.add_argument('--max-steps', type=int, default=25, help='决策步数兜底(回环图)')
    ap.add_argument('--enter-keep', action='store_true', help='跳过清场(调试)')
    ap.add_argument('--enter-phrase', default=None,
                    help='点名进入(书名直进); 多书库下 generic 措辞只得 TTS 菜单,'
                         ' 等不到媒体段——单书库免菜单直进时代的默认契约已过时')
    args = ap.parse_args()
    rng = random.Random(args.seed)
    CACHE_DIR.mkdir(exist_ok=True)

    g = StoryGraph(args.story)
    print(f"[故事图] story={args.story} 节点×{len(g.nodes)} 边×{len(g.edges)} "
          f"start={g.start} ends={g.ends}", flush=True)

    if not args.enter_keep:
        clear_state(args.story)

    c = AipMockClient(BASE_URL, args.device, args.secret)
    assert c.authenticate(), '认证失败（设备不存在用 _smoke_all.py register_device 重注册）'
    assert c.connect(), 'WS 连接失败（gateway 起了吗?）'
    assert c.session_open(), 'session.open 失败'
    feeder = UplinkFeeder(c, load_frames(ensure_silence_frames()))
    feeder.start()
    r = redis_all()[0]

    try:
        run_flow(c, feeder, r, g, rng, args)
    finally:
        feeder.stop()

    fails = [x for x in RESULTS if not x['ok']]
    print('\n' + '=' * 64, flush=True)
    for x in RESULTS:
        print(f"  [{'PASS' if x['ok'] else 'FAIL'}] {x['name']}")
    print('=' * 64)
    print(f"总计 {len(RESULTS)} 项: PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}",
          flush=True)
    traj = ''
    if TRAJECTORY:
        parts = [TRAJECTORY[0]['from'] or '(selecting)']
        for t in TRAJECTORY:
            parts.append('-[' + t['edge'][:24] + ']->' + t['to'])
        traj = ' '.join(parts)
    print(f'\n[轨迹] {traj}', flush=True)
    out = SKILL_DIR / 'story_flow_result.json'
    out.write_text(json.dumps({
        'ok': not fails, 'seed': args.seed, 'story': args.story,
        'trajectory': TRAJECTORY, 'results': RESULTS,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[结果] {out}', flush=True)
    sys.exit(1 if fails else 0)


def run_flow(c, feeder, r, g, rng, args):
    story_id = args.story

    # ── Phase 1: SELECTING 进入（变体重试绕 LRU 缓存; miss=闲聊现编是设计内兜底）
    rec = None
    # 多书库（2026-09 起库里有 3 本 published）：generic 措辞只得 TTS 菜单
    # （无媒体段）；书名措辞一身二职——SELECTING 里是菜单答案、冷启动是
    # tryDirectEntry 点名直进。generic 与书名交替，菜单一出下一句即直进；
    # 分类器偶发降级（fetch failed→闲聊）也被下一措辞自然兜住。
    title_answer = f'{g.title} please' if g.title else None
    enter_phrases = ([args.enter_phrase] if args.enter_phrase else []) \
        + ENTER_PHRASES
    if title_answer:
        enter_phrases = [p for x in enter_phrases for p in (x, title_answer)]
    for phrase in enter_phrases:
        rec = FlowRecorder(c)
        budget = g.nodes[g.start]['frames'] * 0.04 + 90
        speak_and_collect(c, feeder, rec, synth_frames(phrase), min_ends=2,
                          timeout=budget)
        if rec.media_windows():
            check('进入·召回门命中（媒体段出现）', True, f'phrase="{phrase}"')
            break
        print(f'    [重试] 召回门未命中("{phrase}"), 换措辞 ...', flush=True)
    else:
        rec.dump()
        check('进入·召回门命中', False,
              f'{len(enter_phrases)} 个措辞均未进入故事（LLM 精排/超时?）')
        return
    rec.dump()
    check('进入·W810 start/end 契约', not rec.violations,
          '; '.join(rec.violations) or f'{len(rec.windows)} 窗口合法闭合')
    check('进入·无 error.report', not rec.errors, str(rec.errors))
    wins = rec.media_windows()
    if check('进入·媒体段 == start 节点', bool(wins) and
             wins[0]['frames'] == g.nodes[g.start]['frames'],
             f"frames={wins[0]['frames'] if wins else 0} "
             f"expect={g.nodes[g.start]['frames']}"):
        TRAJECTORY.append({'from': '', 'to': g.start, 'edge': '(enter)', 'by': 'enter'})
    uid = discover_uid(r, story_id)
    active, _ = redis_state(r, uid, story_id)
    check('进入·story:active 写入', active == story_id, f'active={active}')

    # ── Phase 2+: 图驱动决策循环
    for step in range(1, args.max_steps + 1):
        if getattr(feeder, 'dead', None) or rec.session_closed and 'lost' in str(rec.session_closed):
            check(f'step{step}·连接存活', False,
                  f'连接断开({feeder.dead or rec.session_closed}) — gateway 重启? '
                  f'查 packages/gateway/logs/gateway/gateway.log 启动行时间戳')
            break
        uid = discover_uid(r, story_id)
        _, sess = redis_state(r, uid, story_id)
        node = sess.get('current_node')
        if node is None:
            if any(t['to'] in g.ends for t in TRAJECTORY):
                break  # 终点已过, exit 已清 hash? (正常 hash 留存; 防御退出)
            check(f'step{step}·current_node 可读', False, 'session hash 丢失')
            break
        edges = g.out_edges(node)
        P = f'step{step}@n{node}'

        if not edges:  # 终点: 上一轮应已播结束语并 exit（异步, 轮询等待）
            active = redis_state(r, uid, story_id)[0]
            for _ in range(10):
                if active is None:
                    break
                time.sleep(3)
                active = redis_state(r, discover_uid(r, story_id), story_id)[0]
            check(f'{P}·终点 exit 后 active 删除', active is None)
            check(f'{P}·session hash 留存(续玩)', bool(sess), str(sess))
            with pg() as conn, conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT current_node_id FROM user_story_progress '
                            'WHERE user_id=%s AND story_id=%s', (uid, story_id))
                prog = cur.fetchone()
            check(f'{P}·PG progress=终点',
                  prog is not None and str(prog['current_node_id']) == node,
                  f"progress={prog['current_node_id'] if prog else None}")
            break

        if len(edges) == 1:
            # 段链应自动连播单出边节点; 停在这里=异常(但仍尝试沿边推进以收集证据)
            check(f'{P}·单出边节点不停留', False,
                  f'仅 1 出边 →{edges[0][0]}（段链连播失效?）')

        target, label = rng.choice(edges)
        run = g.plan_run(target)
        run_frames = sum(g.nodes[n]['frames'] for n in run)
        budget = run_frames * 0.04 + 240  # 余量覆盖 pacer 抖动+路由重试累积
        print(f'    [{P}] 随机选边 "{label}" → {target} '
              f'(预期 run={run}, {run_frames} 帧)', flush=True)

        rec = FlowRecorder(c)
        # 决策答案 = 边语义描述本身(验证 LLM 语义匹配而非字符串相等);
        # miss(澄清)时加 "Let's " 前缀变体重试一次(同句会被 LRU 缓存 none)
        answered = False
        partial = False
        for variant in (label, f"Let's {label.rstrip('!.')}"):
            rec = FlowRecorder(c)
            speak_and_collect(c, feeder, rec, synth_frames(variant),
                              min_ends=2, timeout=budget)
            _, sess2 = redis_state(r, discover_uid(r, story_id), story_id)
            got2 = sess2.get('current_node')
            if got2 == run[-1]:
                answered = True
                break
            if got2 != node and got2 is not None:
                partial = True  # 推进了但 run 未播完(断连/中断), 不值得换变体重试
                break
            print(f'    [重试] 路由 miss(澄清), 换变体 "{variant[:40]}" ...',
                  flush=True)
            rec.dump()  # 每个变体的窗口流都留证（只打最后的会丢中间现场）
        rec.dump()
        check(f'{P}·ASR 识别', bool(rec.asr_finals), f'finals={rec.asr_finals[-1:]}')
        check(f'{P}·W810 start/end 契约', not rec.violations,
              '; '.join(rec.violations) or f'{len(rec.windows)} 窗口合法闭合')
        if answered:
            wins = rec.media_windows()
            expect_run = run
            got_frames = wins[0]['frames'] if wins else 0
            check(f'{P}·媒体段帧数 == ΣframeCount(run={expect_run})',
                  bool(wins) and got_frames == run_frames,
                  f'got {got_frames} expect {run_frames}')
            _, sess3 = redis_state(r, discover_uid(r, story_id), story_id)
            got_node = sess3.get('current_node')
            check(f'{P}·current_node 锚定到 run 末节点',
                  got_node == expect_run[-1], f'{got_node} expect {expect_run[-1]}')
            TRAJECTORY.append({'from': node, 'to': expect_run[-1],
                               'edge': label, 'by': f'random(seed={args.seed})'})
        elif partial:
            check(f'{P}·决策路由推进', False,
                  '已推进但 run 未播完（连接断开/播放中断）— 见上方窗口流')
            break
        else:
            check(f'{P}·决策路由推进', False,
                  f'"{label}" 及变体均未推进（澄清链耗尽?）')
            break  # 无法推进, 终止循环（断言已 FAIL）

        if run[-1] in g.ends:
            # 终点轮: 结束语已播(收集里含), exit 异步清理——补验证在下一循环头
            time.sleep(3)


if __name__ == '__main__':
    main()
