# -*- coding: utf-8 -*-
"""把 table_v1.json.gz 传到阿里云 OSS（国内分发源）。

为什么：客户端直连 GitHub raw 在国内常超时（实测 20 s 都拿不到），
把这张 4 KB 的表放到 OSS 默认域名上，国内直连几十毫秒，且默认域名不需要 ICP 备案。

没配环境变量就安静跳过（exit 0），不会影响发布流程。

用法（在 Actions 里）：
    python upload_oss.py table_v1.json.gz

环境变量：
    OSS_AK / OSS_SK        RAM 用户的 AccessKey（只要 oss:PutObject 权限）
    OSS_BUCKET             bucket 名
    OSS_ENDPOINT           https://oss-cn-hangzhou.aliyuncs.com
    OSS_KEY                对象路径，默认 voidlens/table_v1.json.gz
    OSS_CACHE              Cache-Control，默认 max-age=300（status 用 30）
    OSS_TYPE               Content-Type，默认 application/gzip
    OSS_VERIFY=1           传完 HEAD 一次，确认匿名可读（公共读没配好会立刻发现）
"""

import json
import os
import sys
import time


def write_result(ok, extra=None):
    """把本次上传结果落盘，供后续步骤判断"OSS 到底有没有成功"（continue-on-error 会掩盖失败）。"""
    data = {'ok': bool(ok), 'at': round(time.time(), 1)}
    data.update(extra or {})
    try:
        with open('oss_result.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    except OSError:
        pass


def main(argv):
    path = argv[1] if len(argv) > 1 else 'table_v1.json.gz'
    ak = (os.environ.get('OSS_AK') or '').strip()
    sk = (os.environ.get('OSS_SK') or '').strip()
    bucket_name = (os.environ.get('OSS_BUCKET') or '').strip()
    endpoint = (os.environ.get('OSS_ENDPOINT') or '').strip() or 'https://oss-cn-hangzhou.aliyuncs.com'
    key = (os.environ.get('OSS_KEY') or '').strip() or 'voidlens/table_v1.json.gz'

    if not (ak and sk and bucket_name):
        print('OSS 未配置（缺 OSS_AK / OSS_SK / OSS_BUCKET），跳过上传')
        write_result(False, {'reason': 'not-configured'})
        return 0
    if not os.path.exists(path):
        print('找不到 %s，跳过上传' % path)
        write_result(False, {'reason': 'missing-file'})
        return 0

    import oss2  # 只有真的要传时才需要

    cache = (os.environ.get('OSS_CACHE') or 'max-age=300').strip()
    ctype = (os.environ.get('OSS_TYPE') or 'application/gzip').strip()
    size = os.path.getsize(path)
    bucket = oss2.Bucket(oss2.Auth(ak, sk), endpoint, bucket_name)
    bucket.put_object_from_file(
        key, path,
        headers={'Cache-Control': cache, 'Content-Type': ctype},
    )
    url = '%s/%s/%s' % (endpoint.replace('://', '://%s.' % bucket_name, 1).rstrip('/'),
                        key.split('/')[0] if '/' in key else '', key.split('/', 1)[-1])
    print('OSS OK · %s · %d 字节 · Cache-Control: %s\n客户端地址：%s' % (key, size, cache, url))

    if (os.environ.get('OSS_VERIFY') or '').strip() in ('1', 'true', 'yes'):
        # 匿名 HEAD：桶是公共读、对象存在 → 200。403 就是权限没配对。
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, method='HEAD')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                print('匿名可读 ✓ HTTP %d · %s 字节' % (resp.status, resp.headers.get('Content-Length')))
        except urllib.error.HTTPError as exc:
            print('匿名可读 ✗ HTTP %d —— 检查 Bucket 读权限（应设为公共读）' % exc.code)
            write_result(False, {'reason': 'not-public', 'http': exc.code, 'url': url, 'key': key})
            return 1
    write_result(True, {'url': url, 'key': key, 'bytes': size})
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
