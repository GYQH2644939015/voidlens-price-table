# -*- coding: utf-8 -*-
"""发布端：每小时把 wm 价目表（Prime + 安魂 mod + 青蓝星 / 琥珀星）抓一遍，产出一个表。

产出的表由客户端整份下载（见 vl_cloud.py 的 table 段），口径与程序内 `MarketClient.estimate()`
完全一致：48h 成交量够 → 直接报成交中位；不够 → v2 /top 挂单稳健中位；两边都有 → 6:4 合成。

跑法：

    # 演示（只抓 12 条，别一上来就打 769 条）
    python vl_publish.py --limit 12 --out table.json.gz

    # 正式：每小时一次，769 条约 6–9 分钟（3 请求/秒限流）
    python vl_publish.py --out table.json.gz
    python vl_publish.py --out table.json.gz --push "坚果云|https://dav.jianguoyun.com/dav/VoidLens/|me@x.com|APP_PASS"

给 GitHub Actions 用的时候，把 --out 指到仓库目录，后面 commit 即可（见 `.github/workflows/hourly.yml`）。
"""

import argparse
import calendar
import concurrent.futures
import gzip
import json
import math
import os
import sys
import threading
import time
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vl_cloud as vc            # noqa: E402   （只用 price_key / 编码 / 上传）

MARKET_API = 'https://api.warframe.market/v2'
TRADE_API = 'https://api.warframe.market/v1'
UA = 'VoidLens-Publisher/1.0 (price table)'
REQUEST_TIMEOUT = 20
RATE = 3.0
WORKERS = 8
TABLE_SCHEMA = 'voidlens-table'
TABLE_VERSION = 1

# 安魂 mod：wm 里这 8 个的 slug 就是名字，tags 都是 mod/rare/parazon
REQUIEM_SLUGS = ('fass', 'vome', 'jahu', 'lohk', 'netra', 'ris', 'khra', 'xata')
# 阿耶檀识青蓝星 / 琥珀星
STAR_TAGS = ('ayatan_star',)


class TokenBucket:
    """3 请求/秒的全局节流（与程序内同口径）。"""

    def __init__(self, rate=RATE):
        self.rate = float(rate)
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(0.2, wait))


class Market:
    """只读市场：与 voidlens.py 的 MarketClient 同口径的最小实现。"""

    STATUS_WEIGHT = {'online': 1.0, 'ingame': 1.0, 'offline': 0.35}
    TRADED_CONFIDENT = 8
    OUTLIER_HIGH = 4.0
    OUTLIER_LOW = 1 / 6

    def __init__(self, proxy='', rate=RATE):
        self.bucket = TokenBucket(rate)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Platform': 'pc', 'Language': 'zh-hans'})
        if proxy:
            if '://' not in proxy:
                proxy = 'http://' + proxy
            self.session.proxies = {'http': proxy, 'https': proxy}
        self.requests_made = 0
        self.lock = threading.Lock()

    def get_json(self, url, retries=3, timeout=REQUEST_TIMEOUT):
        for attempt in range(retries):
            self.bucket.acquire()
            with self.lock:
                self.requests_made += 1
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        return None
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(0.8 + attempt * 1.2)
                    continue
                return None
            except Exception:
                time.sleep(0.5 + attempt * 0.8)
        return None

    # ---- 口径（与 voidlens.py 的 MarketClient 逐行一致）----
    @staticmethod
    def _age_days(order, now=None):
        stamp = str(order.get('updatedAt') or order.get('createdAt') or '')[:19]
        try:
            when = calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%S'))
        except Exception:
            return 3.0
        current = (now or datetime.utcnow()).timestamp() if isinstance(now, datetime) else (now or time.time())
        return max(0.0, (float(current) - when) / 86400.0)

    def ask_stats(self, sells, now=None):
        rows = []
        for order in sells or []:
            if not isinstance(order, dict):
                continue
            try:
                price = int(order.get('platinum') or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or order.get('visible') is False:
                continue
            status = str(((order.get('user') or {}).get('status')) or 'offline').lower()
            qty = min(10, max(1, int(order.get('quantity') or 1)))
            weight = self.STATUS_WEIGHT.get(status, self.STATUS_WEIGHT['offline'])
            weight *= qty ** 0.5
            weight *= math.exp(-self._age_days(order, now) / 21.0)
            rows.append({'price': price, 'weight': max(weight, 1e-3), 'status': status})
        if not rows:
            return None
        prices = sorted(r['price'] for r in rows)
        med = prices[len(prices) // 2]
        logs = sorted(math.log(r['price']) for r in rows)
        log_med = logs[len(logs) // 2]
        deviations = sorted(abs(x - log_med) for x in logs)
        mad = deviations[len(deviations) // 2] * 1.4826
        window = max(2.5 * mad, 0.51)
        kept = []
        for row in rows:
            if abs(math.log(row['price']) - log_med) > window:
                continue
            if row['price'] > med * self.OUTLIER_HIGH or row['price'] < med * self.OUTLIER_LOW:
                continue
            kept.append(row)
        if not kept:
            kept = rows
        kept.sort(key=lambda r: r['price'])
        total = sum(r['weight'] for r in kept)
        acc, ref = 0.0, kept[-1]['price']
        for row in kept:
            acc += row['weight']
            if acc >= total / 2:
                ref = row['price']
                break
        online = [r['price'] for r in kept if r['status'] in ('online', 'ingame')]
        return {'ref': ref, 'low_any': kept[0]['price'],
                'low_online': min(online) if online else 0,
                'online': len(online), 'total': len(kept),
                'offline_share': round(1 - len(online) / len(kept), 2)}

    def traded_stats(self, slug):
        payload = (self.get_json(f'{TRADE_API}/items/{slug}/statistics') or {}).get('payload') or {}
        rows = (payload.get('statistics_closed') or {}).get('48hours') or []
        rows = [r for r in rows if (r.get('volume') or 0) > 0 and (r.get('median') or 0) > 0]
        if not rows:
            return None
        volume = sum(int(r['volume']) for r in rows)
        median = sum(float(r['median']) * int(r['volume']) for r in rows) / volume
        return {'median': median, 'volume': volume,
                'low': min(float(r.get('min_price') or median) for r in rows)}

    def top_asks(self, slug):
        data = self.get_json(f'{MARKET_API}/orders/item/{slug}/top') or {}
        sells = (data.get('data') or {}).get('sell')
        return sells if isinstance(sells, list) else None

    def estimate(self, slug):
        traded = self.traded_stats(slug)
        if traded and traded['volume'] >= self.TRADED_CONFIDENT:
            return {'price': max(1, int(round(traded['median']))), 'source': 'traded',
                    'traded': traded, 'ask': None}
        sells = self.top_asks(slug)
        ask = self.ask_stats(sells)
        if not ask:
            if traded:
                return {'price': max(1, int(round(traded['median']))), 'source': 'traded-weak',
                        'traded': traded, 'ask': None}
            return {'price': 0, 'source': 'none', 'traded': traded, 'ask': None}
        price, source = float(ask['ref']), 'ask'
        if traded:
            share = min(0.5, 0.15 * traded['volume'])
            price = math.exp((1 - share) * math.log(max(1.0, price))
                             + share * math.log(max(1.0, traded['median'])))
            source = 'blend'
        return {'price': max(1, int(round(price))), 'source': source,
                'traded': traded, 'ask': ask}


# --------------------------------------------------------------------- 范围
def select_scope(items, mode='prime'):
    """mode=prime：Prime + 安魂 mod + 阿耶檀识青蓝星/琥珀星（769 条）
    mode=full ：物品表里所有条目（几千条，给「每天一次全量表」用）"""
    prime, stars, requiem = [], [], []
    seen = set()
    for item in items or []:
        slug = str(item.get('slug') or '')
        if not slug:
            continue
        tags = [str(t).lower() for t in (item.get('tags') or [])]
        if 'prime' in tags:
            prime.append(slug)
            seen.add(slug)
        elif any(tag in tags for tag in STAR_TAGS):
            stars.append(slug)
            seen.add(slug)
    for slug in REQUIEM_SLUGS:
        if slug not in seen:
            requiem.append(slug)
            seen.add(slug)
    out = {'prime': sorted(set(prime)), 'star': sorted(set(stars)),
           'requiem': list(requiem)}
    if mode == 'full':
        out['full'] = sorted({str(item.get('slug') or '') for item in items or []
                              if str(item.get('slug') or '')})
    return out


def load_items(path=None):
    """本地 market_items.json 优先（省一次 2.6 MB 请求），没有就现拉。"""
    if path and os.path.exists(path):
        payload = vc.read_json(path)
        if isinstance(payload, dict) and payload.get('items'):
            return payload['items']
        if isinstance(payload, list):
            return payload
    market = Market()
    resp = market.session.get(f'{MARKET_API}/items', timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f'拉物品表失败 HTTP {resp.status_code}')
    data = resp.json().get('data')
    if not isinstance(data, list):
        raise SystemExit('物品表格式不对')
    return data


# --------------------------------------------------------------------- 发布
def fetch_prices(market, slugs, workers=WORKERS, progress=None):
    prices, failed, sources = {}, [], {}
    lock = threading.Lock()
    total = len(slugs)
    done = [0]

    def work(slug):
        try:
            est = market.estimate(slug)
        except Exception:
            est = {'price': 0, 'source': 'error'}
        with lock:
            done[0] += 1
            key = vc.price_key(slug)
            if est.get('price'):
                prices[key] = int(est['price'])
                sources[key] = est.get('source') or '?'
            else:
                failed.append(slug)
            if progress and done[0] % 25 == 0:
                progress(done[0], total)
        return slug

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, slugs))
    return prices, failed, sources


def build_table(prices, scope, failed, generated_at=None, sources=None):
    table = {
        'schema': TABLE_SCHEMA, 'v': TABLE_VERSION,
        'generated_at': float(generated_at or time.time()),
        'scope': {k: len(v) for k, v in scope.items()},
        'prices': {k: int(v) for k, v in sorted(prices.items())},
        'missing': sorted(failed),
    }
    if sources:                     # 每条的口径来源只用于排查，正式发布不带（省一半体积）
        table['sources'] = sources
    return table


def encode_table(table):
    payload = dict(table)
    payload['generated_at'] = round(float(payload.get('generated_at') or 0), 0)
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return gzip.compress(raw, 9, mtime=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description='wm 价目表发布端')
    parser.add_argument('--items', help='market_items.json 路径（默认用部署目录里的）')
    parser.add_argument('--scope', choices=('prime', 'full'), default='prime',
                        help='prime=769 条（Prime+安魂+星）；full=物品表全部（几千条）')
    parser.add_argument('--out', default='table_v1.json.gz', help='产出文件')
    parser.add_argument('--limit', type=int, default=0, help='只抓前 N 条（演示 / 试跑）')
    parser.add_argument('--rate', type=float, default=RATE, help='每秒请求上限')
    parser.add_argument('--workers', type=int, default=WORKERS)
    parser.add_argument('--proxy', default='')
    parser.add_argument('--push', action='append', help='顺手上传到 WebDAV：名字|URL|账号|密码')
    parser.add_argument('--max-requests', type=int, default=0, help='硬上限，防跑飞')
    parser.add_argument('--with-sources', action='store_true', help='每条带上口径来源（排查用）')
    args = parser.parse_args(argv)

    default_items = r'C:\Users\Administrator\Desktop\WarframePriceHelperV5.4.1\market_items.json'
    items = load_items(args.items or default_items)
    scope = select_scope(items, args.scope)
    targets = (scope['full'] if args.scope == 'full'
               else scope['prime'] + scope['star'] + scope['requiem'])
    if args.limit:
        # 演示时三种范围都要有：均分取样，保证 requiem / star 不被挤掉
        picks = scope['requiem'][:4] + scope['star'][:2] + scope['prime'][:max(0, args.limit - 6)]
        targets = picks[:args.limit]
    if args.scope == 'full':
        print(f'范围：全量 {len(scope["full"])} 条（其中 Prime {len(scope["prime"])}）'
              f' → 本次抓 {len(targets)} 条')
    else:
        print(f'范围：Prime {len(scope["prime"])} · 星 {len(scope["star"])} '
              f'· 安魂 {len(scope["requiem"])} → 本次抓 {len(targets)} 条')

    market = Market(proxy=args.proxy, rate=args.rate)
    started = time.time()
    prices, failed, sources = fetch_prices(
        market, targets, workers=args.workers,
        progress=lambda done, total: print(f'  … {done}/{total}', flush=True))
    cost = time.time() - started
    if args.max_requests and market.requests_made > args.max_requests:
        print(f'⚠ 请求数 {market.requests_made} 超过上限 {args.max_requests}')

    table = build_table(prices, scope, failed, sources=sources if args.with_sources else None)
    blob = encode_table(table)
    with open(args.out, 'wb') as f:
        f.write(blob)
    print(f'表：{len(prices)} 条 · 失败 {len(failed)} 条 · 请求 {market.requests_made} 次 · '
          f'耗时 {cost:.1f}s · 文件 {len(blob)} 字节')
    if failed:
        print('失败样例：', failed[:8])

    for spec in args.push or []:
        target = vc.Target(spec)
        etag, err = target.put(blob, None, filename='table_v1.json.gz')
        print(f'上传 {target.name}：', 'OK' if not err else err)
    return 0


if __name__ == '__main__':
    sys.exit(main())
