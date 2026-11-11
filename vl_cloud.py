# -*- coding: utf-8 -*-
"""VoidLens 云端同步 · P0 原型（独立脚本，不并入 exe）。

只同步「wm 价格表」：`price_cache.json`（slug → 白金）+ `sync_state.json`（slug → 上次同步时间戳）。
不碰 config.json、不碰 items.json、不碰 market_items.json。

协议：单个远端文件 `voidlens_sync_v1.json`，内容 = 逐键 Last-Write-Wins 合并视图。

    {"schema":"voidlens-sync","v":1,"kind":"prices","device":"...","written":1794...,
     "prices":{"frostprimeset":[179, 1794389019.211]},
     "tomb":{}}                       # tomb：删除墓碑，30 天 GC

合并是「逐键取较新」的 join 运算：幂等、可交换、可结合 —— 所以
  * 任意次数重试结果不变；
  * 两台机器谁先谁后结果一样；
  * 两个后端（坚果云 + InfiniCLOUD）各存一份，互相不一致时下一次同步自动收敛。

用法（P0 只有 CLI，没有 UI）：

    python vl_cloud.py snapshot --dir <数据目录>
    python vl_cloud.py merge    --dirs A B [--apply A] [--out bundle.json]
    python vl_cloud.py sync     --dir <数据目录> --target "坚果云|https://dav.jianguoyun.com/dav/VoidLens/|me@x.com|APP_PASS" \
                                               --target "InfiniCLOUD|https://xxx.infini-cloud.net/dav/VoidLens/|user|PASS"
    python vl_cloud.py test-target --target "..."

只用 stdlib + requests（两者都已在 exe bundle 内），P1 可直接搬进 voidlens.py。
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime
from email import utils as email_utils

import requests

SCHEMA = 'voidlens-sync'
VERSION = 1
KIND = 'prices'
DEFAULT_NAME = 'voidlens_sync_v1.json'
TOMB_TTL = 30 * 24 * 3600          # 墓碑保留 30 天
MAX_PRICE = 10 ** 9
REQUEST_TIMEOUT = (3.05, 12)
USER_AGENT = 'VoidLens/1.0 (cloud-sync P0)'


# --------------------------------------------------------------------- 基础工具
def price_key(slug):
    """与 voidlens.py 完全一致：去下划线 + 小写 + 去首尾空白。"""
    return (slug or '').replace('_', '').lower().strip()


def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path, payload):
    """先写临时文件再替换：同步过程中断电也不会留下半个 JSON。"""
    tmp = f'{path}.tmp-{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def new_device_id():
    return uuid.uuid4().hex[:6]


def _as_price(value):
    try:
        price = int(value)
    except (TypeError, ValueError):
        return 0
    if price <= 0 or price > MAX_PRICE:
        return 0
    return price


def _as_ts(value):
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ts < 0 or ts > 4102444800:      # 2100-01-01 之后的时间戳视为脏数据
        return 0.0
    return ts


# --------------------------------------------------------------------- 快照
def empty_snapshot(device=''):
    return {'schema': SCHEMA, 'v': VERSION, 'kind': KIND, 'device': device or '',
            'written': 0.0, 'prices': {}, 'tomb': {}}


def load_local(dirpath, device=''):
    """把本地 price_cache.json + sync_state.json 读成一份快照（键统一归一）。

    注意：两个文件的键拼写并不一致 —— price_cache 是归一后的 `frostprimeset`，
    sync_state 是原始的 `frost_prime_set`。这里统一按 price_key() 归一。
    """
    snap = empty_snapshot(device)
    cache = read_json(os.path.join(dirpath, 'price_cache.json'))
    stamps = read_json(os.path.join(dirpath, 'sync_state.json'))
    stamps = stamps if isinstance(stamps, dict) else {}

    ts_of = {}
    for key, value in stamps.items():
        nk = price_key(key)
        ts = _as_ts(value)
        if nk and ts > ts_of.get(nk, 0):
            ts_of[nk] = ts

    if isinstance(cache, dict):
        for key, value in cache.items():
            nk = price_key(key)
            price = _as_price(value)
            if nk and price:
                snap['prices'][nk] = [price, ts_of.get(nk, 0.0)]
    # 只有时间戳、没有价格（上次没取到价）的条目不进 bundle：实测 4564 条时间戳里
    # 有 3821 条是"老 Prime、早就不在词典里"的，带上只会把 7 KB 变成 37 KB。
    # 这些条目照旧留在本地 sync_state.json 里，只是不参与云端交换。
    snap['written'] = max(ts_of.values()) if ts_of else 0.0
    return snap


def _ts_key_map(ts_dict):
    """归一键 → 本地文件里实际用的拼写（apply 落盘时沿用，避免同一件东西两个键）。"""
    out = {}
    for key in (ts_dict or {}):
        nk = price_key(key)
        if nk and nk not in out:
            out[nk] = key
    return out


def apply_local(snap, dirpath, device=''):
    """把合并结果写回本地两个文件，返回改动统计。

    * price_cache.json：归一键 → 正整数（与 PriceBook 口径一致）
    * sync_state.json ：沿用本地原有拼写；本地没有的新条目用归一键
    * 墓碑命中的条目从两个文件里删掉
    """
    cache_path = os.path.join(dirpath, 'price_cache.json')
    ts_path = os.path.join(dirpath, 'sync_state.json')
    old_cache = read_json(cache_path)
    old_ts = read_json(ts_path)
    old_cache = old_cache if isinstance(old_cache, dict) else {}
    old_ts = old_ts if isinstance(old_ts, dict) else {}

    raw_of = _ts_key_map(old_ts)
    old_prices = {}
    for key, value in old_cache.items():
        nk = price_key(key)
        price = _as_price(value)
        if nk and price:
            old_prices[nk] = price

    tomb = {price_key(k): _as_ts(v) for k, v in (snap.get('tomb') or {}).items()}

    def killed(slug, ts):
        when = tomb.get(slug)
        return when is not None and when >= ts

    prices = {}
    for slug, rec in (snap.get('prices') or {}).items():
        nk = price_key(slug)
        price, ts = _price_rec(rec)
        if not nk or price <= 0 or killed(nk, ts):
            continue
        prices[nk] = price

    out_ts = {}
    for key, value in old_ts.items():
        ts = _as_ts(value)
        if killed(price_key(key), ts):
            continue
        out_ts[key] = ts
    for slug, rec in (snap.get('prices') or {}).items():
        nk = price_key(slug)
        price, ts = _price_rec(rec)
        if not nk or ts <= 0 or killed(nk, ts):
            continue
        target = raw_of.get(nk, slug)
        if out_ts.get(target) != ts:
            out_ts[target] = ts

    added = sum(1 for k in prices if k not in old_prices)
    changed = sum(1 for k, v in prices.items() if old_prices.get(k) not in (None, v))
    removed = sum(1 for k in old_prices if k not in prices)
    # 键拼写不规整（Frost_Prime_Set）也要顺手纠正，否则文件里会留两份
    need_norm = any(price_key(k) != k for k in old_cache)
    wrote_price = (old_prices != prices) or need_norm
    wrote_ts = old_ts != out_ts

    if wrote_price:
        write_json_atomic(cache_path, {k: prices[k] for k in sorted(prices)})
    if wrote_ts:
        write_json_atomic(ts_path, out_ts)
    return {'price_added': added, 'price_changed': changed, 'price_removed': removed,
            'price_total': len(prices), 'ts_total': len(out_ts),
            'wrote_price': wrote_price, 'wrote_ts': wrote_ts}


# --------------------------------------------------------------------- 合并
def _price_rec(rec):
    """兼容三种写法：[price, ts] / {"p": price, "t": ts} / 裸整数。"""
    if isinstance(rec, dict):
        return _as_price(rec.get('p', rec.get('price'))), _as_ts(rec.get('t', rec.get('ts')))
    if isinstance(rec, (list, tuple)) and len(rec) >= 2:
        return _as_price(rec[0]), _as_ts(rec[1])
    if isinstance(rec, (list, tuple)) and len(rec) == 1:
        return _as_price(rec[0]), 0.0
    return _as_price(rec), 0.0


def merge(*snapshots):
    """逐键 LWW，join 运算：merge(*[A,B]) == merge(*[B,A])，且幂等、可结合。

    同一键比较 (ts, price)：先比时间戳，时间戳相同时取价高者 —— 只是为了
    让结果与输入顺序无关（确定性），不代表"贵的就是对的"。
    """
    out = empty_snapshot('')
    prices = {}
    tomb = {}
    device = ''
    written = 0.0
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        written = max(written, _as_ts(snap.get('written')))
        device = max(device, str(snap.get('device') or ''))
        for slug, rec in (snap.get('prices') or {}).items():
            nk = price_key(slug)
            price, ts = _price_rec(rec)
            if not nk or price <= 0:       # 只有时间戳、没有价：不进 bundle（本地另存）
                continue
            cur = prices.get(nk)
            if cur is None or (ts, price) > (cur[1], cur[0]):
                prices[nk] = [price, ts]
        for slug, value in (snap.get('tomb') or {}).items():
            nk = price_key(slug)
            ts = _as_ts(value)
            if nk and ts > tomb.get(nk, 0):
                tomb[nk] = ts

    horizon = time.time() - TOMB_TTL
    out['tomb'] = {k: v for k, v in tomb.items() if v >= horizon}
    for slug, rec in prices.items():
        killed_at = out['tomb'].get(slug)
        if killed_at is not None and killed_at >= rec[1]:
            continue                       # 已被删除，别再复活
        out['prices'][slug] = rec
    out['device'] = device
    out['written'] = written
    return out


def mark_deleted(snap, slug, ts=None):
    """删除一条价格：从 prices 里摘掉，写墓碑（否则另一台会把它复活）。"""
    nk = price_key(slug)
    if not nk:
        return snap
    snap['prices'].pop(nk, None)
    snap['tomb'][nk] = float(ts if ts is not None else time.time())
    return snap


class Stamp:
    """给"新取到的价格"打时间戳，带时钟护栏。

    本机时钟比云端 `written` 落后（另一台机器快）时，直接用 time.time() 会
    让新取的价格被判成"更旧"从而被远端老价格顶掉。护栏 = 至少比远端新 1 秒。
    """

    def __init__(self, remote_written=0.0):
        self.floor = float(remote_written or 0.0)

    def now(self):
        return max(time.time(), self.floor + 1.0)

    def feed(self, snap):
        self.floor = max(self.floor, _as_ts((snap or {}).get('written')))
        return self


# --------------------------------------------------------------------- 编解码
def encode(snap):
    """bundle 字节串：紧凑 JSON + gzip（4.4 KB 量级）。"""
    payload = {'schema': SCHEMA, 'v': VERSION, 'kind': KIND,
               'device': snap.get('device') or '',
               'written': round(_as_ts(snap.get('written')), 3),
               'prices': {k: [int(v[0]), round(float(v[1]), 3)]
                          for k, v in sorted((snap.get('prices') or {}).items())
                          if int(v[0]) > 0},
               'tomb': {k: round(float(v), 3) for k, v in sorted((snap.get('tomb') or {}).items())}}
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    # mtime=0：同样的内容必须得到同样的字节，否则 ETag 每次都变、"没变化就不推"永远失效
    return gzip.compress(raw, 9, mtime=0)


def decode(blob):
    """返回 (snapshot, error)。任何异常都当"远端不可用"处理，绝不污染本地。"""
    if not blob:
        return None, 'empty'
    try:
        if blob[:2] == b'\x1f\x8b':
            raw = gzip.decompress(blob)
        else:
            raw = blob
        data = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        return None, f'解码失败: {exc.__class__.__name__}'
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        return None, '不是 VoidLens 同步文件'
    try:
        version = int(data.get('v') or 0)
    except (TypeError, ValueError):
        version = 0
    if version > VERSION:
        return None, f'云端版本 v{version} 比本机新，请升级程序'
    snap = empty_snapshot(str(data.get('device') or ''))
    snap['written'] = _as_ts(data.get('written'))
    snap['prices'] = {price_key(k): list(_price_rec(v))
                      for k, v in (data.get('prices') or {}).items() if price_key(k)}
    snap['tomb'] = {price_key(k): _as_ts(v) for k, v in (data.get('tomb') or {}).items()
                    if price_key(k)}
    return snap, None


# --------------------------------------------------------------------- WebDAV
class Target:
    """一个 WebDAV 后端。spec 形如 `名字|URL|账号|密码`（密码可留空，改用环境变量）。"""

    def __init__(self, spec, name=None, url=None, username=None, password=None):
        if spec:
            parts = [p.strip() for p in spec.split('|')]
            parts += [''] * (4 - len(parts))
            name, url, username, password = name or parts[0], url or parts[1], \
                username or parts[2], password or parts[3]
        self.name = name or 'webdav'
        self.root = (url or '').rstrip('/')
        self.username = username or ''
        self.password = password or ''
        self._session = None

    @property
    def key(self):
        return hashlib.sha256(f'{self.name}|{self.root}|{self.username}'.encode()).hexdigest()[:8]

    def url_for(self, filename):
        return f'{self.root}/{filename}' if self.root else filename

    def session(self):
        if self._session is None:
            session = requests.Session()
            session.headers.update({'User-Agent': USER_AGENT})
            if self.username:
                session.auth = (self.username, self.password)
            self._session = session
        return self._session

    def fetch(self, filename=DEFAULT_NAME):
        """返回 (blob, etag, error)；文件不存在 → error='missing'。"""
        try:
            resp = self.session().get(self.url_for(filename), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            return None, None, f'网络错误: {exc.__class__.__name__}'
        if resp.status_code == 404:
            return None, None, 'missing'
        if resp.status_code in (401, 403):
            return None, None, f'认证失败 HTTP {resp.status_code}（检查账号 / 应用密码）'
        if resp.status_code >= 400:
            return None, None, f'HTTP {resp.status_code}'
        return resp.content, resp.headers.get('ETag'), None

    def put(self, blob, etag=None, filename=DEFAULT_NAME):
        """ETag 乐观并发：带 If-Match；还没文件时带 If-None-Match:* 防并发首建。"""
        headers = {'Content-Type': 'application/gzip',
                   'Content-Encoding': 'identity'}
        if etag:
            headers['If-Match'] = etag
        else:
            headers['If-None-Match'] = '*'
        try:
            resp = self.session().put(self.url_for(filename), data=blob,
                                      headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            return None, f'网络错误: {exc.__class__.__name__}'
        if resp.status_code == 412:
            return None, 'conflict'
        if resp.status_code in (401, 403):
            return None, f'认证失败 HTTP {resp.status_code}'
        if resp.status_code in (413, 507):
            return None, f'HTTP {resp.status_code}（远端空间不足）'
        if resp.status_code >= 400:
            return None, f'HTTP {resp.status_code}'
        return resp.headers.get('ETag'), None

    def backup(self, filename=DEFAULT_NAME):
        """远端内容坏了：先另存一份再覆盖，绝不悄悄丢数据。"""
        blob, etag, err = self.fetch(filename)
        if err or not blob:
            return False
        stamp = time.strftime('%Y%m%d-%H%M%S')
        _, put_err = self.put(blob, None, filename=f'{filename}.bak-{stamp}')
        return put_err is None


# --------------------------------------------------------------------- 同步
def sync_once(local_dir, targets, device='', filename=DEFAULT_NAME, rounds=3, log=None):
    """拉所有后端 → 多方合并（本地 + N 个远端）→ 只推有差异的后端。

    返回报告：{'ok':bool,'targets':[...],'device':...,'applied':{...},'pushed':n,'merged':n}

    三条安全规则：
      1. 某个后端"拉"失败（超时 / 401 / 500）→ **绝不推它**，否则会把别人的数据盖掉；
      2. 远端内容坏（不是我们的文件 / 解不开）→ 先备份成 .bak-* 再覆盖；
      3. 推 412（有人抢先写了）→ 重拉、重合并、重推，最多 rounds 轮。
    """
    log = log or (lambda *_: None)
    local = load_local(local_dir, device)
    report = {'ok': True, 'device': local['device'], 'targets': [], 'rounds': 0,
              'pushed': 0, 'merged': 0}
    state = {t.key: {'target': t, 'remote': None, 'etag': None, 'raw': None,
                     'error': None, 'pull_bad': False, 'dirty': True, 'action': 'same'}
             for t in targets}

    for round_no in range(1, rounds + 1):
        report['rounds'] = round_no
        snapshots = [local]
        conflict = False

        # --- 1. 拉
        for key, item in state.items():
            if item['pull_bad']:
                continue
            blob, etag, err = item['target'].fetch(filename)
            if err == 'missing':
                item.update(remote=None, etag=None, raw=None, error=None, pull_bad=False)
                continue
            if err:
                item.update(error=err, pull_bad=True, dirty=True)
                continue
            snap, derr = decode(blob)
            if derr:
                log(f'⚠ {item["target"].name}：{derr}，已备份为 .bak-* 后按空处理')
                item['target'].backup(filename)
                # etag 保留：主文件还在，只是内容不认 —— 用 If-Match 覆盖它
                item.update(remote=None, etag=etag, raw=None, error=None, pull_bad=False)
                continue
            item.update(remote=snap, etag=etag, raw=blob, error=None, pull_bad=False)
            snapshots.append(snap)

        # --- 2. 合并 + 落本地
        merged = merge(*[s for s in snapshots if isinstance(s, dict)])
        merged['device'] = local['device']
        merged['written'] = max((_as_ts(s.get('written')) for s in snapshots
                                 if isinstance(s, dict)), default=0.0)
        report['applied'] = apply_local(merged, local_dir)
        report['merged'] = len(merged['prices'])
        local = merged

        # --- 3. 推（只推与远端不同的；拉失败的跳过）
        for key, item in state.items():
            if item['pull_bad']:
                item['action'] = 'held'
                continue
            blob = encode(merged)
            if item['raw'] == blob:
                item['action'] = 'same'
                item['dirty'] = False
                continue
            etag, err = item['target'].put(blob, item['etag'], filename)
            if err == 'conflict':
                conflict = True
                item.update(error='conflict', dirty=True)
                continue
            if err:
                item.update(error=err, dirty=True)
                continue
            item.update(etag=etag, raw=blob, error=None, dirty=False, action='push')
            report['pushed'] += 1
        if not conflict:
            break
        # 412：下一轮重拉重合并，把冲突方的改动吸收进来

    for key, item in state.items():
        report['targets'].append({'name': item['target'].name, 'action': item['action'],
                                  'error': item['error'], 'bytes': len(item['raw'] or b'')})
        if item['error'] or item['pull_bad']:
            report['ok'] = False
    return report


# --------------------------------------------------------------------- 价目表（发布端 → 客户端）
# 与 P0 的"多机 LWW 互推"是两种用法，共用同一个合并内核：
#   发布端每小时把表做好（vl_publish.py），客户端整份下载，表里每条时间戳 = generated_at。
#   客户端自己直连取到的价时间戳更新，会自然赢过表里的值 —— 不需要额外规则。

TABLE_SCHEMA = 'voidlens-table'
TABLE_VERSION = 1
TABLE_NAME = 'table_v1.json.gz'
TABLE_STATE = 'table_state.json'


def decode_table(blob):
    """返回 (table, error)。表的体积是 4–8 KB，一次下载就是全量。"""
    if not blob:
        return None, 'empty'
    try:
        raw = gzip.decompress(blob) if blob[:2] == b'\x1f\x8b' else blob
        data = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        return None, f'解码失败: {exc.__class__.__name__}'
    if not isinstance(data, dict) or data.get('schema') != TABLE_SCHEMA:
        return None, '不是 VoidLens 价目表'
    try:
        version = int(data.get('v') or 0)
    except (TypeError, ValueError):
        version = 0
    if version > TABLE_VERSION:
        return None, f'表版本 v{version} 比本机新，请升级程序'
    prices = {}
    for slug, value in (data.get('prices') or {}).items():
        key = price_key(slug)
        price = _as_price(value)
        if key and price:
            prices[key] = price
    # 防御：发布端明确标记「这条没抓到」的，即使 prices 里混进了旧值也不要
    for slug in data.get('missing') or []:
        prices.pop(price_key(slug), None)
    if not prices:
        return None, '表里没有价格'
    return {'generated_at': _as_ts(data.get('generated_at')),
            'prices': prices,
            'scope': data.get('scope') or {},
            'missing': list(data.get('missing') or []),
            'sources': data.get('sources') or {}}, None


def table_snapshot(table):
    """把表当成一份"所有条目都在 generated_at 抓的"快照，交给 merge 处理。"""
    snap = empty_snapshot('table')
    ts = _as_ts(table.get('generated_at'))
    skip = {price_key(k) for k in (table.get('missing') or [])}
    snap['written'] = ts
    snap['prices'] = {price_key(k): [int(v), ts]
                      for k, v in (table.get('prices') or {}).items()
                      if price_key(k) not in skip}
    return snap


def apply_table(table, dirpath):
    """表 → 本地。本地更新（直连实时价）或表里没有的条目都不动。"""
    local = load_local(dirpath)
    merged = merge(local, table_snapshot(table))
    merged['device'] = local.get('device') or ''
    stats = apply_local(merged, dirpath)
    stats['table_generated_at'] = _as_ts(table.get('generated_at'))
    stats['kept_newer_local'] = sum(
        1 for k, v in local['prices'].items()
        if v[1] > _as_ts(table.get('generated_at')) and k in (table.get('prices') or {}))
    return stats


def http_date(stamp):
    return email_utils.formatdate(float(stamp or time.time()), usegmt=True)


def table_state(dirpath):
    state = read_json(os.path.join(dirpath, TABLE_STATE))
    return state if isinstance(state, dict) else {}


def save_table_state(dirpath, **fields):
    state = table_state(dirpath)
    state.update({k: v for k, v in fields.items() if v is not None})
    write_json_atomic(os.path.join(dirpath, TABLE_STATE), state)
    return state


def fetch_table(urls, state=None, timeout=15, log=None):
    """按顺序尝试多个源（CDN / raw / 坚果云），第一个成功就用。

    返回 (blob, url, not_modified)。带 If-Modified-Since，命中 304 时返回 not_modified=True。
    """
    log = log or (lambda *_: None)
    state = state or {}
    headers = {'User-Agent': USER_AGENT}
    since = state.get('fetched_http') or state.get('fetched_header')
    if since:
        headers['If-Modified-Since'] = since
    errors = []
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            errors.append(f'{url} → {exc.__class__.__name__}')
            continue
        if resp.status_code == 304:
            return None, url, True
        if resp.status_code != 200:
            errors.append(f'{url} → HTTP {resp.status_code}')
            continue
        return resp.content, url, False
    log('云表下载失败：' + '；'.join(errors))
    return None, None, False


def pull_table(dirpath, urls, force=False, log=None, timeout=15):
    """客户端一次"取表 → 落地"，返回报告 dict。"""
    state = table_state(dirpath)
    blob, url, not_modified = fetch_table(urls, state, timeout=timeout, log=log)
    report = {'source': url, 'not_modified': not_modified, 'applied': None, 'error': None}
    if not_modified:
        report['applied'] = {'skipped': 'not-modified'}
        return report
    if not blob:
        report['error'] = '拿不到表'
        return report
    table, err = decode_table(blob)
    if err:
        report['error'] = err
        return report
    generated = _as_ts(table.get('generated_at'))
    if not force and generated <= _as_ts(state.get('generated_at')):
        report['applied'] = {'skipped': 'already-newer', 'generated_at': generated}
        save_table_state(dirpath, source=url, fetched_at=time.time(),
                         fetched_http=http_date(time.time()))
        return report
    report['applied'] = apply_table(table, dirpath)
    now = time.time()
    save_table_state(dirpath, generated_at=generated, source=url, fetched_at=now,
                     fetched_http=http_date(now), count=len(table.get('prices') or {}),
                     bytes=len(blob))
    return report


# --------------------------------------------------------------------- CLI
def _targets(specs):
    out = []
    for spec in specs or []:
        target = Target(spec)
        if not target.password:
            env = 'VLC_PASS_' + target.name.upper().replace('-', '_').replace(' ', '_')
            target.password = os.environ.get(env, '')
        out.append(target)
    return out


def cmd_snapshot(args):
    snap = load_local(args.dir)
    print(json.dumps({'dir': args.dir, 'prices': len(snap['prices']),
                      'with_ts': sum(1 for v in snap['prices'].values() if v[1] > 0),
                      'gzip_bytes': len(encode(snap))}, ensure_ascii=False, indent=2))


def cmd_merge(args):
    snaps = [load_local(d) for d in args.dirs]
    merged = merge(*snaps)
    if args.out:
        with open(args.out, 'wb') as f:
            f.write(encode(merged))
    if args.apply:
        stats = apply_local(merged, args.apply)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(json.dumps({'inputs': len(snaps), 'merged': len(merged['prices']),
                      'tomb': len(merged['tomb']), 'gzip_bytes': len(encode(merged))},
                     ensure_ascii=False, indent=2))


def cmd_sync(args):
    report = sync_once(args.dir, _targets(args.target), device=args.device or new_device_id(),
                       log=lambda line: print(line))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 2


def cmd_table_apply(args):
    if args.file:
        with open(args.file, 'rb') as f:
            blob = f.read()
        table, err = decode_table(blob)
        if err:
            print(json.dumps({'error': err}, ensure_ascii=False))
            return 2
        stats = apply_table(table, args.dir)
        save_table_state(args.dir, generated_at=table['generated_at'], source=args.file,
                         fetched_at=time.time(), fetched_http=http_date(time.time()),
                         count=len(table['prices']), bytes=len(blob))
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if not args.url:
        print('要么 --file，要么 --url')
        return 2
    report = pull_table(args.dir, args.url, force=args.force,
                        log=lambda line: print(line))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report['error'] else 2


def cmd_table_info(args):
    with open(args.file, 'rb') as f:
        blob = f.read()
    table, err = decode_table(blob)
    if err:
        print(json.dumps({'error': err}, ensure_ascii=False))
        return 2
    stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(table['generated_at']))
    print(json.dumps({'generated_at': table['generated_at'], 'utc': stamp,
                      'prices': len(table['prices']), 'scope': table['scope'],
                      'missing': len(table['missing']), 'bytes': len(blob)},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_test_target(args):
    for target in _targets(args.target):
        blob, etag, err = target.fetch(args.name)
        if err == 'missing':
            print(f'{target.name}: 连接正常，远端还没有 {args.name}（首次同步会创建）')
            continue
        if err:
            print(f'{target.name}: 失败 —— {err}')
            continue
        snap, derr = decode(blob)
        print(f'{target.name}: 连接正常，{len(blob)} 字节，'
              f'{"解析失败: " + derr if derr else str(len(snap["prices"])) + " 条价格"}')
    return 0


def cmd_pull(args):
    targets = _targets(args.target)
    snapshots = [load_local(args.dir)]
    for target in targets:
        blob, etag, err = target.fetch(args.name)
        if err:
            print(f'{target.name}: {err if err != "missing" else "远端为空"}')
            continue
        snap, derr = decode(blob)
        if derr:
            print(f'{target.name}: {derr}')
            continue
        snapshots.append(snap)
    merged = merge(*snapshots)
    print(json.dumps(apply_local(merged, args.dir), ensure_ascii=False, indent=2))
    return 0


def cmd_push(args):
    report = sync_once(args.dir, _targets(args.target), device=args.device or new_device_id(),
                       log=lambda line: print(line))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 2


def build_parser():
    parser = argparse.ArgumentParser(description='VoidLens 云端同步 P0（价格表 LWW）')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('snapshot', help='看看本地快照有多大')
    p.add_argument('--dir', required=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser('merge', help='合并多个目录（离线，不联网）')
    p.add_argument('--dirs', nargs='+', required=True)
    p.add_argument('--apply', help='把合并结果写回这个目录')
    p.add_argument('--out', help='同时导出 bundle 文件')
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser('sync', help='拉 + 合并 + 推（多后端）')
    p.add_argument('--dir', required=True)
    p.add_argument('--target', action='append', help='名字|URL|账号|密码（密码可放环境变量）')
    p.add_argument('--device', default='')
    p.add_argument('--name', default=DEFAULT_NAME)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser('pull', help='只拉不推')
    p.add_argument('--dir', required=True)
    p.add_argument('--target', action='append', required=True)
    p.add_argument('--name', default=DEFAULT_NAME)
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser('push', help='拉 + 合并 + 推（同 sync，语义上强调推送）')
    p.add_argument('--dir', required=True)
    p.add_argument('--target', action='append', required=True)
    p.add_argument('--device', default='')
    p.add_argument('--name', default=DEFAULT_NAME)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser('table-apply', help='下载云端价目表并落地（发布端产出的表）')
    p.add_argument('--dir', required=True)
    p.add_argument('--url', action='append', help='表的地址，可给多个（按顺序回退）')
    p.add_argument('--file', help='本地表文件，不走网络')
    p.add_argument('--force', action='store_true', help='即使不比本地新也应用')
    p.set_defaults(func=cmd_table_apply)

    p = sub.add_parser('table-info', help='看一张表的内容')
    p.add_argument('--file', required=True)
    p.set_defaults(func=cmd_table_info)

    p = sub.add_parser('test-target', help='只检查连通性 / 凭据')
    p.add_argument('--target', action='append', required=True)
    p.add_argument('--name', default=DEFAULT_NAME)
    p.set_defaults(func=cmd_test_target)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
