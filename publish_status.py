# -*- coding: utf-8 -*-
"""把本班发布结果写成 publish_status.json（<1 KB），随表一起传到 OSS。

为什么要有它：过去"云端停更"永远是事后才发现的——客户端只知道自己手里那张表
有多旧，不知道是**没叫醒**、**抓取失败**还是**没传上去**。这个文件把三件事写进去：

    generated_at   表自己的生成时间（价格数据的名义时间）
    written_at     这次落盘的时间（发布端最后一次成功的证据）
    run_id/run_url 对应哪次 Actions 运行（点进去就能看日志）
    missing/count  这一班抓坏了多少条（>80 基本就是出口 IP 被拦或 wm 抽风）

用法（在工作流里，`always()` 跑，失败也写）：

    python publish_status.py table_v1.json.gz publish_status.json

本地排查也一样能用：它只读文件，不联网。
"""

import gzip
import json
import os
import sys
import time

SERVER = os.environ.get('GITHUB_SERVER_URL') or 'https://github.com'


def load(path):
    """解表；坏表不抛异常，把错误写进状态里（宁可状态难看，也不要整班挂掉）。"""
    try:
        blob = open(path, 'rb').read()
    except OSError as exc:
        return {}, 0, 'unreadable: %s' % exc.__class__.__name__
    try:
        raw = gzip.decompress(blob) if blob[:2] == b'\x1f\x8b' else blob
        return json.loads(raw.decode('utf-8')), len(blob), ''
    except Exception as exc:                       # noqa: BLE001 —— 任何解析错都记下来
        return {}, len(blob), '%s: %s' % (exc.__class__.__name__, str(exc)[:120])


def build(table_path, error=''):
    table, size, err = load(table_path)
    prices = table.get('prices') or {}
    scope = table.get('scope') or {}
    repo = os.environ.get('GITHUB_REPOSITORY') or ''
    run_id = os.environ.get('GITHUB_RUN_ID') or ''
    status = {
        'schema': 'voidlens-publish-status',
        'v': 1,
        'ok': bool(prices) and not (err or error),
        'written_at': round(time.time(), 1),
        'generated_at': round(float(table.get('generated_at') or 0), 1),
        'count': len(prices),
        'missing': len(table.get('missing') or []),
        'scope': {k: int(v) for k, v in scope.items()},
        'table_bytes': size,
        'repo': repo,
        'run_id': run_id,
        'run_url': ('%s/%s/actions/runs/%s' % (SERVER, repo, run_id)) if run_id else '',
        'event': os.environ.get('GITHUB_EVENT_NAME') or '',
        'actor': os.environ.get('GITHUB_TRIGGERING_ACTOR') or '',
        # 双触发器并行时分辨是谁叫醒的（cronjob / cloudflare / fc / manual）
        'via': (os.environ.get('TRIGGER_VIA') or '').strip()[:32],
    }
    note = err or error
    if note:
        status['error'] = note
    return status


def main(argv):
    table_path = argv[1] if len(argv) > 1 else 'table_v1.json.gz'
    out_path = argv[2] if len(argv) > 2 else 'publish_status.json'
    status = build(table_path, os.environ.get('PUBLISH_ERROR') or '')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, separators=(',', ':'))
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status['ok'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
