# voidlens-price-table

VoidLens（虚空透镜）用的 **warframe.market 价目表**，每小时由 GitHub Actions 自动生成一次。

- **范围**：Prime 全量 759 条 + 安魂 mod 8 条 + 阿耶檀识青蓝星 / 琥珀星 2 条 = **769 条**
- **产出**：`table_v1.json.gz`，**约 3.4 KB**
- **取价口径**：与 VoidLens 内 `MarketClient.estimate()` 完全一致
  （48h 成交量够 → 报成交中位；不够 → `/top` 稳健挂单中位；两边都有 → 6:4 合成）
- **成本**：0 元。抓取用 public 仓库的免费 Actions 分钟数，分发用 jsDelivr 免费 CDN

## 客户端怎么用

VoidLens 里填这个地址（设置 → 云端同步 → 价目表地址）：

```
https://cdn.jsdelivr.net/gh/<你的用户名>/voidlens-price-table@main/table_v1.json.gz
https://raw.githubusercontent.com/<你的用户名>/voidlens-price-table/main/table_v1.json.gz
```

首选 jsDelivr（国内可达），失败自动退到 raw。

命令行手动应用（P1 的 exe 还没接进来之前可以这么用）：

```bash
python vl_cloud.py table-apply --dir "C:\Users\Administrator\Desktop\WarframePriceHelperV5.4.1" \
  --url https://cdn.jsdelivr.net/gh/<用户名>/voidlens-price-table@main/table_v1.json.gz
```

## 部署（一次性，3 步）

1. 在 GitHub 建一个 **public** 空仓库，名字就叫 `voidlens-price-table`
   （**必须 public**：private 仓库的 Actions 只有 2000 分钟/月，每小时一跑约 720×7 = 5000 分钟，会超）
2. 把本目录整个推上去：

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<用户名>/voidlens-price-table.git
git push -u origin main
```

3. 打开仓库 → **Actions** 标签 → 选 `hourly-price-table` → **Run workflow** 手动跑一次
   （约 5–6 分钟，跑完仓库里会出现 `table_v1.json.gz`）

之后每小时第 7 分钟自动跑，git 提交新表，并调用 jsDelivr 的 purge 接口清 CDN 缓存。

## 每小时的负载

| 项 | 数值 |
|---|---|
| 请求数 | 约 900–1150 次（多数条目 1 次 `/v1/.../statistics` 就够） |
| 限流 | 3 请求/秒（wm 官方建议上限），并发 8 |
| 耗时 | 约 5–6.5 分钟 |
| 平均速率 | 0.28 请求/秒，是建议上限的十分之一 |

改周期：`.github/workflows/hourly.yml` 里的 `cron`，例如每 2 小时 `7 */2 * * *`。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 大面积 `missing`（> 80 条） | wm 前面有 Cloudflare，可能拦了 GitHub 的出口 IP | 看 Actions 日志里的 `missing` 列表；退回本机跑 `python vl_publish.py`（家用宽带 IP 不会被拦） |
| 出现 429 | 限流撞上了 | 把 `--rate 3` 降到 `--rate 2`，或周期改 2 小时 |
| 客户端拿到旧表 | jsDelivr 对分支引用有缓存（最长约 12 小时） | workflow 每次都会 purge；仍不对就改用 `raw.githubusercontent.com` 地址 |
| Actions 没按时跑 | GitHub 的 cron 是尽力而为，高峰会延迟 | 对小时级表无影响；客户端会显示“表生成于 HH:MM” |

## 文件

| 文件 | 作用 |
|---|---|
| `vl_publish.py` | 抓 769 条 → 生成 `table_v1.json.gz` |
| `vl_cloud.py` | 客户端合并内核 + `table-info` / `table-apply` 命令 |
| `check_table.py` | 跑完检查表是否健康（条目数 / 失败数 / 体积） |
| `.github/workflows/hourly.yml` | 每小时定时任务 |
