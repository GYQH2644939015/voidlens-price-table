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
"""

import os
import sys


def main(argv):
    path = argv[1] if len(argv) > 1 else 'table_v1.json.gz'
    ak = (os.environ.get('OSS_AK') or '').strip()
    sk = (os.environ.get('OSS_SK') or '').strip()
    bucket_name = (os.environ.get('OSS_BUCKET') or '').strip()
    endpoint = (os.environ.get('OSS_ENDPOINT') or '').strip() or 'https://oss-cn-hangzhou.aliyuncs.com'
    key = (os.environ.get('OSS_KEY') or '').strip() or 'voidlens/table_v1.json.gz'

    if not (ak and sk and bucket_name):
        print('OSS 未配置（缺 OSS_AK / OSS_SK / OSS_BUCKET），跳过上传')
        return 0
    if not os.path.exists(path):
        print('找不到 %s，跳过上传' % path)
        return 0

    import oss2  # 只有真的要传时才需要

    size = os.path.getsize(path)
    bucket = oss2.Bucket(oss2.Auth(ak, sk), endpoint, bucket_name)
    bucket.put_object_from_file(
        key, path,
        headers={'Cache-Control': 'max-age=300', 'Content-Type': 'application/gzip'},
    )
    url = '%s/%s/%s' % (endpoint.replace('://', '://%s.' % bucket_name, 1).rstrip('/'),
                        key.split('/')[0] if '/' in key else '', key.split('/', 1)[-1])
    print('OSS OK · %s · %d 字节\n客户端地址：%s' % (key, size, url))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
