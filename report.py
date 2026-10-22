"""Read-only net performance report for signals.json (no network calls)."""
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime

import tracker

WINS = {tracker.TOOK_PROFIT, tracker.BREAKEVEN_AFTER_TP1}
LOSSES = {tracker.STOPPED_OUT}
NEUTRAL = {tracker.TIMEOUT, tracker.TREND_REVERSED}


def _dt(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def load_records(include_archive=False):
    store = tracker.load_signals()
    records = store.get('signals', [])
    if include_archive:
        try:
            with open(tracker.ARCHIVE_PATH, encoding='utf-8') as handle:
                records = records + json.load(handle).get('signals', [])
        except (OSError, json.JSONDecodeError):
            pass
    return store.get('stats_epoch'), records


def wilson(wins, total, z=1.96):
    if total <= 0:
        return None
    p = wins / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _r(sig):
    net, risk = sig.get('pnl_net_pct'), sig.get('risk_pct')
    if net is None or risk is None:
        return None
    try:
        risk = float(risk)
        return float(net) / risk if risk > 0 else None
    except (TypeError, ValueError):
        return None


def empty_bucket():
    return {'n': 0, 'wins': 0, 'losses': 0, 'neutral': 0, 'net_pnl': 0.0,
            'r_values': [], 'mfe': [], 'mae': [], 'hold_hours': []}


def add_to_bucket(bucket, sig):
    bucket['n'] += 1
    reason = sig.get('exit_reason')
    if reason in WINS:
        bucket['wins'] += 1
    elif reason in LOSSES:
        bucket['losses'] += 1
    elif reason in NEUTRAL:
        bucket['neutral'] += 1
    net = sig.get('pnl_net_pct')
    if net is not None:
        bucket['net_pnl'] += float(net)
    r = _r(sig)
    if r is not None:
        bucket['r_values'].append(r)
    for key, source in (('mfe', 'mfe'), ('mae', 'mae')):
        value = sig.get(source)
        if isinstance(value, (int, float)):
            bucket[key].append(float(value))
    filled = _dt(sig.get('entry_filled_at') or sig.get('triggered_at'))
    closed = _dt(sig.get('last_checked_at'))
    if filled and closed:
        bucket['hold_hours'].append((closed - filled).total_seconds() / 3600)


def summarize(bucket):
    n = bucket['n']
    rs = bucket['r_values']
    wins = bucket['wins']
    losses = bucket['losses']
    avg_r = sum(rs) / len(rs) if rs else None
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r < 0]
    gross_win = sum(win_r)
    gross_loss = abs(sum(loss_r))
    max_streak, streak = 0, 0
    for r in rs:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    ci = wilson(wins, n)
    return {
        'n': n, 'wins': wins, 'losses': losses, 'neutral': bucket['neutral'],
        'win_rate': wins / n if n else None,
        'wilson95': ci,
        'net_pnl_pct': bucket['net_pnl'] if n else None,
        'avg_r': avg_r,
        'avg_win_r': sum(win_r) / len(win_r) if win_r else None,
        'avg_loss_r': sum(loss_r) / len(loss_r) if loss_r else None,
        'expectancy_r': avg_r,
        'profit_factor': gross_win / gross_loss if gross_loss > 0 else None,
        'max_consecutive_losses': max_streak,
        'avg_hold_hours': (sum(bucket['hold_hours']) / len(bucket['hold_hours'])
                           if bucket['hold_hours'] else None),
        'avg_mfe': sum(bucket['mfe']) / len(bucket['mfe']) if bucket['mfe'] else None,
        'avg_mae': sum(bucket['mae']) / len(bucket['mae']) if bucket['mae'] else None,
    }


def build_report(records, epoch=None, last=None):
    epoch = _dt(epoch) if epoch else None
    main = empty_bucket()
    not_entered = 0
    excluded = {'not_entered': 0, 'manual_reset': 0, 'not_eligible': 0,
                'legacy_no_net': 0, 'before_epoch': 0}
    versions = defaultdict(empty_bucket)
    universes = defaultdict(empty_bucket)
    directions = defaultdict(empty_bucket)
    months = defaultdict(empty_bucket)
    eligible = []
    for sig in records:
        if sig.get('status') != 'exited':
            continue
        reason = sig.get('exit_reason')
        triggered = _dt(sig.get('triggered_at'))
        if reason == tracker.NOT_ENTERED:
            not_entered += 1
            excluded['not_entered'] += 1
            continue
        if reason == tracker.MANUAL_RESET or sig.get('stats_eligible') is False:
            excluded['not_eligible' if reason != tracker.MANUAL_RESET else 'manual_reset'] += 1
            continue
        if sig.get('pnl_net_pct') is None:
            excluded['legacy_no_net'] += 1
            continue
        if epoch and triggered and triggered < epoch:
            excluded['before_epoch'] += 1
            continue
        eligible.append(sig)
    if last:
        eligible = sorted(eligible, key=lambda s: s.get('triggered_at') or '')[-last:]
    eligible.sort(key=lambda s: s.get('last_checked_at') or s.get('triggered_at') or '')
    batches = defaultdict(list)
    for sig in eligible:
        add_to_bucket(main, sig)
        add_to_bucket(versions[sig.get('strategy_version') or 'legacy'], sig)
        add_to_bucket(universes[sig.get('universe_version') or 'unknown'], sig)
        add_to_bucket(directions[sig.get('direction') or 'unknown'], sig)
        month = (sig.get('triggered_at') or '')[:7] or 'unknown'
        add_to_bucket(months[month], sig)
        triggered = _dt(sig.get('triggered_at'))
        if triggered:
            window = triggered.replace(minute=triggered.minute // 10 * 10,
                                       second=0, microsecond=0)
            batches[window.isoformat()].append(sig)
    history = [s for s in records
               if s.get('status') == 'exited' and s.get('exit_reason') != tracker.NOT_ENTERED
               and (s.get('triggered_at') or '')[:10] and s not in eligible]
    history_gross = sum(float(s.get('pnl_now') or 0) for s in history)
    total_outcomes = main['n'] + not_entered
    return {
        'generated_at': datetime.now(tracker.BJ_TZ).isoformat(),
        'stats_epoch': epoch.isoformat() if epoch else None,
        'main': summarize(main),
        'not_entered': not_entered,
        'not_entered_rate': not_entered / total_outcomes if total_outcomes else None,
        'excluded': excluded,
        'versions': {key: summarize(value) for key, value in sorted(versions.items())},
        'universes': {key: summarize(value) for key, value in sorted(universes.items())},
        'directions': {key: summarize(value) for key, value in sorted(directions.items())},
        'months': {key: summarize(value) for key, value in sorted(months.items())},
        'history': {'n': len(history), 'gross_pnl_pct': history_gross,
                    'note': '毛收益口径，未扣成本，不参与主统计'},
        'batches': [
            {'window_start': key, 'n': len(items),
             'symbols': [s['symbol'] for s in items]}
            for key, items in sorted(batches.items()) if len(items) > 1
        ],
        'observation_only': main['n'] < 30,
    }


def _fmt(value, digits=2, suffix=''):
    if value is None:
        return '—'
    return f'{value:.{digits}f}{suffix}'


def _pct(value):
    return '—' if value is None else f'{value * 100:.1f}%'


def render_markdown(report):
    main = report['main']
    ci = main['wilson95']
    lines = [
        '# Crypto Radar 净口径报表',
        '',
        f"- 生成时间：{report['generated_at']}",
        f"- 统计起点：{report['stats_epoch'] or '未设置'}",
        f"- 有效平仓样本：**{main['n']}**"
        + ('（观察中，N<30）' if report['observation_only'] else ''),
        f"- 胜/负/中性：{main['wins']} / {main['losses']} / {main['neutral']}",
        f"- 胜率：{_pct(main['win_rate'])}"
        + (f"，Wilson 95% [{ci[0]*100:.1f}%, {ci[1]*100:.1f}%]" if ci else ''),
        f"- 单笔期望：{_fmt(main['expectancy_r'], 2, 'R')}；"
        f"平均盈利 {_fmt(main['avg_win_r'], 2, 'R')}；"
        f"平均亏损 {_fmt(main['avg_loss_r'], 2, 'R')}",
        f"- 盈亏比：{_fmt(main['profit_factor'], 2)}；"
        f"最大连续不利：{main['max_consecutive_losses']} 笔",
        f"- 累计净收益：{_fmt(main['net_pnl_pct'], 2, '%')}；"
        f"平均持仓 {_fmt(main['avg_hold_hours'], 1, 'h')}；"
        f"MFE/MAE {_fmt(main['avg_mfe'], 2, '%')} / {_fmt(main['avg_mae'], 2, '%')}",
        f"- 未成交：{report['not_entered']} 笔，"
        f"未成交率 {_pct(report['not_entered_rate'])}（不计入胜率分母）",
        f"- 历史参考：{report['history']['n']} 笔，毛收益 "
        f"{_fmt(report['history']['gross_pnl_pct'], 2, '%')}"
        f"（{report['history']['note']}）",
        '',
        '## 版本切片',
        '',
        '| 版本 | N | 胜率 | 期望 R | 净收益% |',
        '|---|---:|---:|---:|---:|',
    ]
    for version, item in report['versions'].items():
        lines.append(f"| {version} | {item['n']} | {_pct(item['win_rate'])} | "
                     f"{_fmt(item['expectancy_r'], 2)} | {_fmt(item['net_pnl_pct'], 2)} |")
    lines += ['', '## 标的池版本', '', '| 版本 | N | 胜率 | 期望 R |',
              '|---|---:|---:|---:|']
    for version, item in report['universes'].items():
        lines.append(f"| {version} | {item['n']} | {_pct(item['win_rate'])} | "
                     f"{_fmt(item['expectancy_r'], 2)} |")
    lines += ['', '## 方向', '', '| 方向 | N | 胜率 | 期望 R |', '|---|---:|---:|---:|']
    for direction, item in report['directions'].items():
        lines.append(f"| {direction} | {item['n']} | {_pct(item['win_rate'])} | "
                     f"{_fmt(item['expectancy_r'], 2)} |")
    lines += ['', '## 按月', '', '| 月份 | N | 胜率 | 期望 R |', '|---|---:|---:|---:|']
    for month, item in report['months'].items():
        lines.append(f"| {month} | {item['n']} | {_pct(item['win_rate'])} | "
                     f"{_fmt(item['expectancy_r'], 2)} |")
    lines += ['', '## 并发批次', '',
              '同一 10 分钟窗口内触发的信号视为并发批次；未做相关性调整，'
              '胜率 Wilson 区间在并发样本下偏窄。', '']
    if report['batches']:
        lines += ['| 窗口起点 | 单数 | 币种 |', '|---|---:|---|']
        for batch in report['batches']:
            lines.append(f"| {batch['window_start']} | {batch['n']} | "
                         f"{', '.join(batch['symbols'])} |")
    else:
        lines.append('（无并发批次）')
    lines += ['', '## 排除项', '']
    for key, value in report['excluded'].items():
        lines.append(f'- {key}: {value}')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='crypto-radar net performance report')
    parser.add_argument('--json', action='store_true', help='output JSON')
    parser.add_argument('--last', type=int, default=None, help='only last N eligible closed signals')
    parser.add_argument('--archive', action='store_true', help='include archived signals')
    args = parser.parse_args(argv)
    epoch, records = load_records(args.archive)
    report = build_report(records, epoch=epoch, last=args.last)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))


if __name__ == '__main__':
    main()
