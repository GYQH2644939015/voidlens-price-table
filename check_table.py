# -*- coding: utf-8 -*-
"""跑完自检：这张表还能用吗？

    python check_table.py table_v1.json.gz

检查项：能不能解开、条目数是否接近 769、失败条目是否过多、体积是否异常。
失败条目的判定不看单条（个别下架商品本来就没价），只看比例：
  * > 80 条失败 → 打 GitHub 警告（多半是出口 IP 被拦或 wm 在抽风）
  * 一条价格都没有 → 退出码 2（这种表宁可别提交）
"""

import gzip
import json
import sys

EXPECTED = 769
MAX_MISSING = 80
MAX_BYTES = 32 * 1024


def main(path):
    with open(path, 'rb') as f:
        blob = f.read()
    try:
        raw = gzip.decompress(blob) if blob[:2] == b'\x1f\x8b' else blob
        table = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        print(f'::error::表解不开：{exc}')
        return 2

    prices = table.get('prices') or {}
    missing = table.get('missing') or []
    generated = float(table.get('generated_at') or 0)
    print(f'条目 {len(prices)} / 期望 {EXPECTED}')
    print(f'失败 {len(missing)} 条')
    print(f'生成时间戳 {generated:.0f}')
    print(f'体积 {len(blob)} 字节')

    if not prices:
        print('::error::表里一条价格都没有，不提交')
        return 2
    if len(blob) > MAX_BYTES:
        print(f'::warning::表体积 {len(blob)} 字节，超过预期（正常约 3.4 KB）')
    if len(missing) > MAX_MISSING:
        print(f'::warning::有 {len(missing)} 条取价失败 —— 可能是 wm 拦了 GitHub 的出口 IP，'
              f'确认后请改在本机跑 vl_publish.py')
    if len(prices) < EXPECTED * 0.8:
        print(f'::warning::条目数只有 {len(prices)}，明显偏少')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'table_v1.json.gz'))
