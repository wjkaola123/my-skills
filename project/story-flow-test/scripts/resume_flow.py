# -*- coding: utf-8 -*-
"""阶段二（今天）：验证中途断点续播语义（按 AG-20 时序图设计契约）。

前置：阶段一（story_flow.py --story N --seed S --max-steps 2）已把进度
断在中间节点并断连（进程退出即 WS 掉线，模拟设备关机/隔天）。

设计契约（时序图 SELECTING 期·用户 2026-08-31 裁决"先问是正确行为"）：
新会话（session.open 冷启动）+ 有未完存档 →
  T1 "Tell me a story" → 召回门 → SELECTING → 查 PG 未完存档 →
     先问"上次《…》玩到一半，继续吗？"（TTS 段，无媒体）
  T2 "继续吧"（≈resumeKeepLabel）→ classify 候选={继续,换一个} →
     enter(resumeNodeId=断点)：断点节点媒体整段重播 + 复述其决策问题
  T3+ 图驱动答到终点 → 锚定推进正常、终点 exit 清 active、PG=终点

⚠ 前置（N-30 重设计后）：阶段一断连后须等 story:active 指针自然过期
  （TTL 600s，anchor 逐节点续期）再跑——指针活着时冷启动是「静默恢复」
  （不出声不开播，首句进已恢复的 PLAYING 会话走 offtopic/续播语义），
  T1 的先问断言只对指针过期后的路径成立：AG-32「冷启动先确认」= 指针
  过期（回灌不触发）+ storyTakeover 一律带 resumeEntry（先问"上次《X》
  要接着听吗"）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import story_flow as sf  # noqa: E402

STORY = int(sys.argv[1]) if len(sys.argv) > 1 else 83

ASK_KEYWORDS = ('继续', '一半', '接着', 'keep going', 'halfway', 'pick up')
# 确认语要足够长（ASR 才收得全）且明确肯定——裸 "Yes." 会被闭世界判 none
CONFIRM_VARIANTS = [
    'Yes please, keep going with the last story',
    'I want to continue the story from where we stopped',
    'Sure, let us keep going with it',
]


def state_snapshot(g):
    r = sf.redis_all()[0]
    uid = sf.discover_uid(r, STORY)
    active, sess = sf.redis_state(r, uid, STORY)
    node = sess.get('current_node') if sess else None
    with sf.pg() as conn, conn.cursor() as cur:
        cur.execute('SELECT current_node_id FROM user_story_progress '
                    'WHERE user_id=%s AND story_id=%s', (uid, STORY))
        row = cur.fetchone()
    pg_node = str(row[0]) if row else None
    return r, uid, active, node, pg_node


def tts_text(rec):
    return ' '.join(w.get('agent_text') or '' for w in rec.windows
                    if w['kind'] == 'tts')


def main():
    g = sf.StoryGraph(STORY)
    r, uid, active, node, pg_node = state_snapshot(g)
    print(f"[昨天断点] redis current_node={node} active={active} "
          f"pg={pg_node} (start={g.start} ends={g.ends})", flush=True)
    sf.check('断点·Redis current_node 存在', node is not None, str(node))
    sf.check('断点·非终点节点', node not in g.ends, str(node))
    sf.check('断点·PG progress == Redis current_node', pg_node == node,
             f'pg={pg_node} redis={node}')

    c = sf.AipMockClient(sf.BASE_URL, sf.DEVICE_ID, sf.DEVICE_SECRET)
    assert c.authenticate(), '认证失败'
    assert c.connect() and c.session_open(), 'WS 连接失败'
    feeder = sf.UplinkFeeder(c, sf.load_frames(sf.ensure_silence_frames()))
    feeder.start()
    try:
        # ── T1（设计契约）：先问"继续吗"，本回合不播媒体
        rec = sf.FlowRecorder(c)
        sf.speak_and_collect(c, feeder, rec,
                             sf.synth_frames('Tell me a story'),
                             min_ends=2, timeout=90)
        rec.dump()
        ask = tts_text(rec)
        sf.check('T1·续播先确认（无媒体段）', not rec.media_windows(),
                 f'media={len(rec.media_windows())} tts="{ask[:60]}"')
        sf.check('T1·resumeAsk 话术命中',
                 any(k in ask for k in ASK_KEYWORDS), ask[:80])

        # ── T2（设计契约）：确认继续 → 从断点节点重播 + 复述其决策问题
        expect_frames = g.nodes[node]['frames']
        budget = expect_frames * 0.04 + 120
        resumed = False
        for phrase in CONFIRM_VARIANTS:
            rec2 = sf.FlowRecorder(c)
            sf.speak_and_collect(c, feeder, rec2, sf.synth_frames(phrase),
                                 min_ends=2, timeout=budget)
            m2 = rec2.media_windows()
            _, _, _, got_node, _ = state_snapshot(g)
            if m2 and got_node == node:
                resumed = True
                break
            # miss 后迟到的澄清 TTS（LLM 判决 3~8s）会把下一变体的上行音频
            # 撞进下行门丢弃——先排空再试
            time.sleep(9)
            print(f'    [重试] 确认语 miss("{phrase[:40]}"), 换变体 ...',
                  flush=True)
        rec2.dump()
        sf.check('T2·确认后从断点重播该节点媒体',
                 resumed and m2 and m2[0]['frames'] == expect_frames,
                 f'frames={m2[0]["frames"] if m2 else 0} '
                 f'expect={expect_frames}(n{node})；若=833(n{g.start})则为从头重播')
        sf.check('T2·断点决策问题复述', bool(tts_text(rec2)),
                 tts_text(rec2)[:80])

        # ── T3+: 图驱动答到终点（沿用 run_flow 的选边/锚定断言形态）
        cur_node = node
        for step in range(1, 7):
            edges = g.out_edges(cur_node)
            if not edges:
                break
            target, label = edges[0]
            run = g.plan_run(target)
            run_frames = sum(g.nodes[n]['frames'] for n in run)
            budget = run_frames * 0.04 + 240
            print(f'    [T{step + 1}@n{cur_node}] 选边 "{label}" → {target} '
                  f'(预期 run={run}, {run_frames} 帧)', flush=True)
            answered = False
            for variant in (label, f"Let's {label.rstrip('!.')}"):
                rec3 = sf.FlowRecorder(c)
                sf.speak_and_collect(c, feeder, rec3, sf.synth_frames(variant),
                                     min_ends=2, timeout=budget)
                _, _, _, got, _ = state_snapshot(g)
                if got == run[-1]:
                    answered = True
                    break
                if got != cur_node and got is not None:
                    break
            rec3.dump()
            p = f'T{step + 1}@n{cur_node}'
            sf.check(f'{p}·ASR 识别', bool(rec3.asr_finals),
                     f'finals={rec3.asr_finals[-1:]}')
            sf.check(f'{p}·W810 start/end 契约', not rec3.violations,
                     '; '.join(rec3.violations) or
                     f'{len(rec3.windows)} 窗口合法闭合')
            m3 = rec3.media_windows()
            sf.check(f'{p}·媒体段帧数 == ΣframeCount(run={run})',
                     bool(m3) and m3[0]['frames'] == run_frames,
                     f'got {m3[0]["frames"] if m3 else 0} expect {run_frames}')
            sf.check(f'{p}·current_node 锚定到 run 末节点', answered,
                     f'{run[-1]}')
            if not answered:
                break
            cur_node = run[-1]
            if cur_node in g.ends:
                time.sleep(3)
                rr, uidd, active2, node2, pg2 = state_snapshot(g)
                sf.check('终点·exit 后 active 删除', active2 is None,
                         f'active={active2}')
                sf.check('终点·PG progress=终点', pg2 == cur_node,
                         f'pg={pg2}')
                break
    finally:
        feeder.stop()

    fails = [x for x in sf.RESULTS if not x['ok']]
    print('\n' + '=' * 64, flush=True)
    for x in sf.RESULTS:
        print(f"  [{'PASS' if x['ok'] else 'FAIL'}] {x['name']}")
    print('=' * 64)
    print(f'总计 {len(sf.RESULTS)} 项: '
          f'PASS {len(sf.RESULTS) - len(fails)} / FAIL {len(fails)}', flush=True)
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
