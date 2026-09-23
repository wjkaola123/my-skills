"""story_exit_test.py — 故事中途退出语义测试（"I don't want to hear it"）

复用 story_flow.py 全部基础设施（常开麦/合成/清场/断言），流程变体：
  1. 进入故事（与全流程脚本同）
  2. 图驱动走 N 步（默认 1）随机决策边 → 到达**中间决策点**
  3. 在决策点说 "I don't want to hear it" → 断言 exit('explicit') 全语义：
     ①级确定性正则 dontwantto(hear|...) 命中（不进 LLM，无 LRU 缓存问题）
     - ASR 识别该话语
     - 下行 = bye 桥接话术 TTS（无媒体段：cancel 停掉故事播报）
     - story:active:{uid} 删除；story:session hash 留存（存档供续玩）
     - PG user_story_progress 存档 current_node=退出锚定节点 +
       last_user_transcript=退出话语
  4. 回归：退出后再说闲聊 → 无媒体段（不再进故事链路）、主链路闲聊回应、
     active 仍为空

用法:
  python story_exit_test.py                    # 默认 story 83, 走 1 步后退出
  python story_exit_test.py --steps 2 --seed 7
结果: 控制台矩阵 + ../story_exit_result.json
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPTS_DIR))
import story_flow as sf  # noqa: E402  (复用其基础设施/断言)

EXIT_PHRASE = 'I don\'t want to hear it'
CHITCHAT_PHRASE = 'How is the weather today'   # 无 story 词, 不会误触召回门


def check(name, ok, detail=''):
    return sf.check(name, ok, detail)


def read_progress(uid, story_id):
    with sf.pg() as conn, conn.cursor() as cur:
        cur.execute('SELECT current_node_id, last_user_transcript, flags '
                    'FROM user_story_progress WHERE user_id=%s AND story_id=%s',
                    (uid, story_id))
        return cur.fetchone()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--story', default='83')
    ap.add_argument('--device', default=sf.DEVICE_ID)
    ap.add_argument('--secret', default=sf.DEVICE_SECRET)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--steps', type=int, default=1, help='退出前走的决策步数')
    args = ap.parse_args()
    rng = random.Random(args.seed)
    sf.CACHE_DIR.mkdir(exist_ok=True)

    g = sf.StoryGraph(args.story)
    print(f"[故事图] story={args.story} 节点×{len(g.nodes)} 边×{len(g.edges)} "
          f"start={g.start} ends={g.ends}", flush=True)
    sf.clear_state(args.story)

    c = sf.AipMockClient(sf.BASE_URL, args.device, args.secret)
    assert c.authenticate(), '认证失败'
    assert c.connect(), 'WS 连接失败'
    assert c.session_open(), 'session.open 失败'
    feeder = sf.UplinkFeeder(c, sf.load_frames(sf.ensure_silence_frames()))
    feeder.start()
    r = sf.redis_all()[0]

    try:
        run(c, feeder, r, g, rng, args)
    finally:
        feeder.stop()

    fails = [x for x in sf.RESULTS if not x['ok']]
    print('\n' + '=' * 64, flush=True)
    for x in sf.RESULTS:
        print(f"  [{'PASS' if x['ok'] else 'FAIL'}] {x['name']}"
              + (f' — {x['detail']}' if x['detail'] else ''))
    print('=' * 64)
    print(f"总计 {len(sf.RESULTS)} 项: PASS {len(sf.RESULTS) - len(fails)} / FAIL {len(fails)}",
          flush=True)
    out = SCRIPTS_DIR.parent / 'story_exit_result.json'
    out.write_text(json.dumps({'ok': not fails, 'phrase': EXIT_PHRASE,
                               'trajectory': sf.TRAJECTORY, 'results': sf.RESULTS},
                              ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[结果] {out}', flush=True)
    sys.exit(1 if fails else 0)


def run(c, feeder, r, g, rng, args):
    story_id = args.story

    # ── Phase 1: 进入故事（与全流程脚本同：变体重试绕 LRU）──
    rec = None
    for phrase in sf.ENTER_PHRASES:
        rec = sf.FlowRecorder(c)
        budget = g.nodes[g.start]['frames'] * 0.04 + 90
        sf.speak_and_collect(c, feeder, rec, sf.synth_frames(phrase),
                             min_ends=2, timeout=budget)
        if rec.media_windows():
            break
        print(f'    [重试] 召回门未命中("{phrase}"), 换措辞 ...', flush=True)
    else:
        rec.dump()
        check('进入·召回门命中（媒体段出现）', False, '3 个措辞均未进入故事')
        return
    rec.dump()
    check('进入·召回门命中（媒体段出现）', True, f'phrase="{phrase}"')
    wins = rec.media_windows()
    check('进入·媒体段 == start 节点',
          bool(wins) and wins[0]['frames'] == g.nodes[g.start]['frames'],
          f"frames={wins[0]['frames'] if wins else 0} "
          f"expect={g.nodes[g.start]['frames']}")
    uid = sf.discover_uid(r, story_id)
    active, _ = sf.redis_state(r, uid, story_id)
    check('进入·story:active 写入', active == story_id, f'active={active}')

    # ── Phase 2: 走 N 步随机决策边 → 中间决策点 ──
    node = None
    for step in range(1, args.steps + 1):
        uid = sf.discover_uid(r, story_id)
        _, sess = sf.redis_state(r, uid, story_id)
        node = sess.get('current_node')
        edges = g.out_edges(node)
        if not edges:
            check(f'step{step}·途中到达终点(图太短)', False,
                  f'n{node} 已是终点, 无法测中途退出 — 减少 --steps')
            return
        target, label = rng.choice(edges)
        run_ = g.plan_run(target)
        run_frames = sum(g.nodes[n]['frames'] for n in run_)
        budget = run_frames * 0.04 + 240
        print(f"    [step{step}@n{node}] 随机选边 \"{label}\" → {target} "
              f"(预期 run={run_}, {run_frames} 帧)", flush=True)
        answered = False
        for variant in (label, f"Let's {label.rstrip('!.')}"):
            rec = sf.FlowRecorder(c)
            sf.speak_and_collect(c, feeder, rec, sf.synth_frames(variant),
                                 min_ends=2, timeout=budget)
            got = sf.redis_state(r, sf.discover_uid(r, story_id), story_id)[1].get('current_node')
            if got == run_[-1]:
                answered = True
                break
            if got != node and got is not None:
                break
            print(f'    [重试] 路由 miss(澄清), 换变体 ...', flush=True)
        rec.dump()
        check(f'step{step}·ASR 识别', bool(rec.asr_finals),
              f"finals={rec.asr_finals[-1:]}")
        check(f'step{step}·决策路由推进到 n{run_[-1]}', answered,
              f'got={got}')
        if not answered:
            return
        node = run_[-1]
        sf.TRAJECTORY.append({'from': None, 'to': run_[-1], 'edge': label,
                              'by': f'random(seed={args.seed})'})
    print(f"[中途] 已到达中间决策点 n{node}, 注入退出话语: \"{EXIT_PHRASE}\"",
          flush=True)

    # ── Phase 3: 中途退出（核心被测对象）──
    P = f'exit@n{node}'
    rec = sf.FlowRecorder(c)
    sf.speak_and_collect(c, feeder, rec, sf.synth_frames(EXIT_PHRASE),
                         min_ends=1, timeout=120)
    rec.dump()
    check(f'{P}·ASR 识别退出话语',
          any(('hear' in f.lower()) or ('want' in f.lower())
              for f in rec.asr_finals),
          f"finals={rec.asr_finals}")
    check(f'{P}·无媒体段（exit 后不再播故事内容）',
          not rec.media_windows(), f"media×{len(rec.media_windows())}")
    tts = rec.tts_windows()
    check(f'{P}·收到 TTS 回应（bye 桥接话术）', bool(tts),
          f"tts×{len(tts)}")
    if tts:
        bye = ' '.join(w['agent_text'] or '' for w in tts)
        check(f'{P}·话术为道别而非澄清/复述提问',
              any(k in bye.lower() for k in
                  ('all for now', 'again', 'bye', 'goodbye', 'see you'))
              or ('下次' in bye or '到这里' in bye),
              f'"{bye[:80]}"')
    check(f'{P}·W810 start/end 契约', not rec.violations,
          '; '.join(rec.violations) or f'{len(rec.windows)} 窗口合法闭合')
    check(f'{P}·无 error.report', not rec.errors, str(rec.errors))

    uid = sf.discover_uid(r, story_id)
    active = r.get(f'story:active:{uid}')
    for _ in range(10):  # exit 异步, 轮询兜底
        if active is None:
            break
        time.sleep(3)
        uid = sf.discover_uid(r, story_id)
        active = r.get(f'story:active:{uid}')
    check(f'{P}·story:active 删除（引擎已退出）', active is None,
          f'active={active}')
    _, sess = sf.redis_state(r, uid, story_id)
    check(f'{P}·session hash 留存（存档供续玩）', bool(sess), str(sess)[:80])
    prog = read_progress(uid, story_id)
    check(f'{P}·PG progress 存档到退出节点',
          prog is not None and str(prog[0]) == node,
          f"progress={prog[0] if prog else None} expect={node}")
    check(f'{P}·last_user_transcript=退出话语',
          prog is not None and 'hear' in (prog[1] or '').lower(),
          f'transcript={prog[1] if prog else None!r}')

    # ── Phase 4: 回归 — 退出后闲聊不应再进故事链路 ──
    rec4 = sf.FlowRecorder(c)
    sf.speak_and_collect(c, feeder, rec4, sf.synth_frames(CHITCHAT_PHRASE),
                         min_ends=1, timeout=120)
    rec4.dump()
    check('回归·闲聊无媒体段（不再进故事链路）',
          not rec4.media_windows(), f"media×{len(rec4.media_windows())}")
    check('回归·主链路存活（闲聊 TTS 回应）', bool(rec4.tts_windows()),
          f"tts×{len(rec4.tts_windows())}")
    active2 = r.get(f'story:active:{sf.discover_uid(r, story_id)}')
    check('回归·story:active 仍为空', active2 is None, f'active={active2}')


if __name__ == '__main__':
    main()
