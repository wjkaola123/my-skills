r"""aip-client-test 技能公共件——从 _smoke_moderation.py 抽取的既证范式（2026-09-02）。

生成的冒烟脚本开头这样接入（两条 sys.path 一个都不能少）:

    import sys
    # Windows: r'D:\Tools' + r'D:\Projects\.zcode\skills\aip-client-test\scripts'
    sys.path.insert(0, '/mnt/d/Tools')                                            # aip_client 包 (WSL)
    sys.path.insert(0, '/home/wj/project/.claude/skills/aip-client-test/scripts') # 本模块 (WSL)
    from smoke_common import *

提供: 设备/用户常量、语音合成缓存链、帧加载、连接、manual/continuous 两模式轮次、
gateway.log 增量读取、PG 画像读写、check/结果落盘。
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import time

IS_WIN = os.name == 'nt'
if IS_WIN:
    BASE = r'D:\Tools\aip_client'
    GATEWAY_LOG = r'D:\Projects\agent\packages\gateway\logs\gateway\gateway.log'
    ENCODE_JS = r'C:\Users\T\.agents\skills\story-flow-test\scripts\encode_frames.js'
    AGENT_DIR = r'D:\Projects\agent'
else:  # WSL2 视图：aip_client 在 /mnt/d，agent 仓在 ~/project，TTS 走 powershell.exe 互通
    BASE = '/mnt/d/Tools/aip_client'
    GATEWAY_LOG = '/home/wj/project/agent/packages/gateway/logs/gateway/gateway.log'
    # encode_frames.js 在相邻技能 story-flow-test 下——从本文件位置推导（scripts→aip-client-test→skills），claude/zcode 两侧部署都成立
    ENCODE_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             'story-flow-test', 'scripts', 'encode_frames.js')
    AGENT_DIR = '/home/wj/project/agent'
CACHE = os.path.join(BASE, '.cache_smoke')

# 两台 mock 设备（aip_devices 表；secret 为 dev 明文）。user_id=NULL 时画像落在设备 id 上。
DEV1 = dict(base_url='http://127.0.0.1:3000',
            device_id='mock-w810-001', device_secret='mock-secret-123456')
DEV2 = dict(base_url='http://127.0.0.1:3000',
            device_id='mock-w810-002', device_secret='mock-secret-654321')
USER1 = 'fb49ce90-9e7b-4534-a5c4-d7a1cf9508c2'  # mock-w810-001
USER2 = '5d99e80b-50a1-4579-bd32-f3f38b2f3431'  # mock-w810-002

CHECKS = []


# ─── 语音合成: PowerShell System.Speech → wav → node opusscript → .frames ───────
def _win_path(p: str) -> str:
    """WSL 路径 '/mnt/d/a/b' → 'D:\\a\\b'（powershell.exe 互通需 Windows 视图路径）。
    wav/ps1 都落在 CACHE（/mnt/d 下），两侧视图都能访问。"""
    m = re.match(r'^/mnt/([a-z])(/.*)$', p)
    if not m:
        return p
    return m.group(1).upper() + ':' + m.group(2).replace('/', '\\')


def synth_frames(text: str, lang: str) -> str:
    """文本 → .frames 路径（md5(lang|text) 缓存，重复句子零成本）。
    lang 跟着文本走（zh→Huihui / en→Zira），与设备语言设置无关——ASR 双语都认。"""
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.md5(f'{lang}|{text}'.encode()).hexdigest()[:16]
    frames_path = os.path.join(CACHE, f'{key}.frames')
    if os.path.exists(frames_path):
        return frames_path
    wav = os.path.join(CACHE, f'{key}.wav')
    voice = 'Microsoft Huihui Desktop' if lang == 'zh' else 'Microsoft Zira Desktop'
    esc = text.replace("'", "''")
    ps1 = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('{voice}')
$s.Rate = -1
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{_win_path(wav)}', $fmt)
$s.Speak('{esc}')
$s.SetOutputToNull()
"""
    ps1_path = os.path.join(CACHE, f'{key}.ps1')
    open(ps1_path, 'w', encoding='utf-8-sig').write(ps1)
    ps_exe = 'powershell' if IS_WIN else 'powershell.exe'
    ps_kw = {} if IS_WIN else {'cwd': BASE}  # WSL 下 cwd 落 /mnt/d，避免互操作目录告警
    subprocess.run([ps_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', _win_path(ps1_path)], check=True, capture_output=True,
                   timeout=60, **ps_kw)
    env = dict(os.environ, AGENT_DIR=AGENT_DIR)
    r = subprocess.run(['node', ENCODE_JS, wav, frames_path],
                       capture_output=True, timeout=120, env=env)
    assert r.returncode == 0, f'encode 失败: {r.stderr[:300]}'
    return frames_path


def load_frames(path):
    data = open(path, 'rb').read()
    frames, off = [], 0
    while off + 4 <= len(data):
        (n,) = struct.unpack_from('<I', data, off)
        off += 4
        frames.append(data[off:off + n])
        off += n
    return frames


def _silence_tail():
    p = os.path.join(BASE, '_silence.frames')
    if os.path.exists(p):
        return load_frames(p)[:20]  # 20 帧 × 60ms ≈ 1.2s，触发 VAD 断句
    from aip_client.audio import generate_silence_frames
    return generate_silence_frames(1.2)


# ─── 连接与轮次 ─────────────────────────────────────────────────────────────────
def connect(dev):
    from aip_client import AipMockClient
    c = AipMockClient(**dev)
    assert c.authenticate(), '认证失败'
    assert c.connect(), 'WS 连接失败'
    assert c.session_open(), '会话开启失败'
    return c


def manual_turn(c, text, lang, timeout=60, save_downlink=False):
    """manual 轮次：显式 voice.turn.start/end 包夹，适合确定性单轮断言。"""
    from aip_client.codec import make_envelope
    frames = load_frames(synth_frames(text, lang))
    c.turn_id += 1
    tid = c.turn_id
    c.ws.send(json.dumps(make_envelope('voice.turn.start', {'mode': 'manual', 'input': ['audio']},
                                       session_id=c.session_id, turn_id=tid)))
    c._send_audio_frames_realtime(frames)
    c.ws.send(json.dumps(make_envelope('voice.turn.end',
                                       {'reason': 'end_of_speech', 'audio_frames': len(frames)},
                                       session_id=c.session_id, turn_id=tid)))
    return c._wait_voice_response(tid, timeout=timeout, save_downlink=save_downlink,
                                  turn_end_ts=int(time.time() * 1000))


def continuous_turn(c, text, lang, timeout=60):
    """持续流模式（生产主形态）：无 turn.start/end，靠尾部静音 VAD 断句；
    投机执行（ASR_SPECULATIVE_LLM）只在该路径活跃。"""
    frames = load_frames(synth_frames(text, lang)) + _silence_tail()
    c._send_audio_frames_realtime(frames, send_fin=False)
    return c._wait_voice_response(None, timeout=timeout,
                                  turn_end_ts=int(time.time() * 1000))


# ─── gateway.log 增量读取（turn 前记录偏移，turn 后取新行） ─────────────────────
def log_size():
    """未开 GATEWAY_LOCAL_LOG_ENABLED 的环境（如 WSL 本地 dev）文件不存在 → None，
    log_read(None) 返回空列表，日志源证据自动缺席而非报错。"""
    return os.path.getsize(GATEWAY_LOG) if os.path.exists(GATEWAY_LOG) else None


def log_read(off):
    if off is None or not os.path.exists(GATEWAY_LOG):
        return []
    with open(GATEWAY_LOG, 'rb') as f:
        f.seek(off)
        return f.read().decode('utf-8', 'replace').splitlines()


def parse_log_events(lines, device_id):
    """返回 (pino 事件 dict 列表, [AipHub/<device> turnLog 原始行])。"""
    events, hub_lines = [], []
    for ln in lines:
        if ln.startswith('{'):
            try:
                events.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        elif f'[AipHub/{device_id}' in ln:
            hub_lines.append(ln)
    return events, hub_lines


# ─── PG: robot_user_profile ────────────────────────────────────────────────────
PG_DSN = 'postgresql://agent:dev_password@localhost:5432/agent_cluster'


def pg():
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(PG_DSN)
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def get_profile(user_id, key='preference.audience'):
    conn, cur = pg()
    try:
        cur.execute("select user_id,key,value,confidence,source,evidence,updated_at "
                    "from robot_user_profile where user_id=%s and key=%s", (user_id, key))
        return cur.fetchone()
    finally:
        conn.close()


def clear_profile(user_id):
    """清档纪律：测试写画像的用例，跑前清、跑后按需清——mock 设备是共享资源。"""
    conn, cur = pg()
    try:
        cur.execute("delete from robot_user_profile where user_id=%s and key='preference.audience'",
                    (user_id,))
        conn.commit()
        print(f'  已清除 {user_id} 的 preference.audience（{cur.rowcount} 行）')
    finally:
        conn.close()


def wait_profile(user_id, want='child', timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = get_profile(user_id)
        if row and row['value'] == want:
            return row
        time.sleep(2)
    return get_profile(user_id)


# ─── 断言与结果落盘 ─────────────────────────────────────────────────────────────
def check(name, cond, detail=''):
    tag = 'PASS' if cond else 'FAIL'
    CHECKS.append({'name': name, 'ok': bool(cond), 'detail': str(detail)[:500]})
    print(f'  [{tag}] {name}' + (f' — {detail}' if detail else ''))
    return bool(cond)


def save_result(path):
    data = json.loads(open(path, encoding='utf-8').read()) if os.path.exists(path) else {}
    data.setdefault('runs', []).append({
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'checks': CHECKS,
    })
    open(path, 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False, indent=1))
    print(f'→ 结果已写入 {path}（本轮 {sum(1 for c in CHECKS if c["ok"])}/{len(CHECKS)} 过）')
