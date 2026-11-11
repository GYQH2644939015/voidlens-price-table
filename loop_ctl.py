# -*- coding: utf-8 -*-
"""价目表「接力链」控制器。

背景：这个仓库的 GitHub Actions `schedule` 从建仓起一条运行记录都没有
（2026-11-11 12:02Z ~ 11-12 03:00Z，三条 workflow 全 0，而 push / 手动都是秒级），
所以发布端不能靠 cron，改成 workflow_dispatch 自动接力：

    每一班跑完（或一开始）用 GITHUB_TOKEN 派下一班 -> 下一班自己睡到下一个整点 :07 再抓表

依据：官方文档「使用 GITHUB_TOKEN 执行任务时，由它触发的事件不会产生新的 workflow 运行，
例外是 workflow_dispatch 与 repository_dispatch」——所以我们能自己叫醒自己。

子命令：
    wait      睡到下一个整点的 :07（UTC），最多 60 分钟
    handoff   单飞守卫 + 派下一班（失败重试 3 次）
    info      打印本班信息
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

WORKFLOW = 'price-loop.yml'
SLOT_MINUTE = 7
ACTIVE = {'queued', 'in_progress', 'waiting', 'pending', 'requested'}


def repo():
    return os.environ.get('GITHUB_REPOSITORY') or 'GYQH2644939015/voidlens-price-table'


def gh_api(path):
    out = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return json.loads(out.stdout)


def cmd_wait():
    now = datetime.now(timezone.utc)
    target = now.replace(minute=SLOT_MINUTE, second=0, microsecond=0)
    if target <= now + timedelta(seconds=30):
        target += timedelta(hours=1)
    delay = (target - now).total_seconds()
    print('现在 %sZ，等到 %sZ（%.1f 分钟）' % (now.strftime('%H:%M:%S'),
                                             target.strftime('%H:%M:%S'), delay / 60.0),
          flush=True)
    while delay > 0:
        step = min(delay, 600)
        time.sleep(step)
        delay -= step
        print('  还剩 %.1f 分钟' % (max(delay, 0) / 60.0), flush=True)
    print('到点，开始抓表', flush=True)
    return 0


def cmd_handoff():
    me = str(os.environ.get('GITHUB_RUN_ID', '0'))
    runs = gh_api('repos/%s/actions/workflows/%s/runs?per_page=30' % (repo(), WORKFLOW))
    others = [r for r in (runs.get('workflow_runs') or [])
              if str(r.get('id')) != me and r.get('status') in ACTIVE]
    if others:
        print('已有别的班次在跑/排队，本班不再派（防止链条分叉）：')
        for r in others[:5]:
            print('  run %s  %s  %s' % (r.get('id'), r.get('status'), r.get('created_at')))
        return 0

    last = ''
    for attempt in range(1, 4):
        out = subprocess.run(['gh', 'workflow', 'run', WORKFLOW, '-f', 'phase=chain'],
                             capture_output=True, text=True)
        if out.returncode == 0:
            print('已派出下一班 %s（第 %d 次尝试）' % (WORKFLOW, attempt))
            return 0
        last = (out.stderr or out.stdout).strip()[:300]
        print('派出失败（第 %d 次）：%s' % (attempt, last), flush=True)
        time.sleep(20)
    print('::error::三次都没能派出下一班，链条断了（改 LOOP_KICK 再 push 一次即可重启）')
    return 1


def cmd_info():
    print('repo    %s' % repo())
    print('run id  %s' % os.environ.get('GITHUB_RUN_ID'))
    print('event   %s' % os.environ.get('GITHUB_EVENT_NAME'))
    print('now     %sZ' % datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
    return 0


def main(argv):
    table = {'wait': cmd_wait, 'handoff': cmd_handoff, 'info': cmd_info}
    if len(argv) < 2 or argv[1] not in table:
        print(__doc__)
        return 2
    return table[argv[1]]()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
