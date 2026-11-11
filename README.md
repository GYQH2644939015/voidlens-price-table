# voidlens-price-table

VoidLens（虚空透镜）用的 **warframe.market 全量价目表**，每天由 GitHub Actions 自动生成一次。

- **范围**：物品表里**全部 3893 条**（Prime 759 + 安魂 mod 8 + 阿耶檀识星 2 + 其余武器/塑像/遗物/杂项）
- **产出**：`table_v1.json.gz`，**约 30 KB**
- **周期**：每天 12:07 UTC（20:07 北京）；一次约 4000–5800 次请求、25–35 分钟
- **取价口径**：与 VoidLens 内 `MarketClient.estimate()` 完全一致
  （48h 成交量够 → 报成交中位；不够 → `/top` 稳健挂单中位；两边都有 → 6:4 合成）
- **成本**：0 元。抓取用 public 仓库的免费 Actions 分钟数，分发用 jsDelivr 免费 CDN

## 客户端怎么用

VoidLens 里填这个地址（设置 → 云端同步 → 价目表地址）：

```
https://cdn.jsdelivr.net/gh/<你的用户名>/voidlens-price-table@main/table_v1.json.gz
https://raw.githubusercontent.com/<你的用户名>/voidlens-price-table/main/table_v1.json.gz
```

顺序建议：**先 raw、后 jsDelivr**。

- `raw.githubusercontent.com` 每小时的 :07 生成、约 :13 就能拿到（实测滞后 5 分钟）；
- `cdn.jsdelivr.net` 由 `purge-cdn.yml` 在 :25 刷新，滞后约 20 分钟，但国内可达性好；
- 客户端两个都配，前一个失败自动退到下一个。

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

3. 打开仓库 → **Actions** 标签 → 选 `price-table` → **Run workflow** 手动跑一次
   （约 25–35 分钟，跑完仓库里会出现 `table_v1.json.gz`）

之后每天 12:07 UTC 自动跑、12:25 UTC 自动清 CDN 缓存（`purge-cdn.yml`）。

> 为什么分两个任务：GitHub 自己的 `raw.githubusercontent.com` 有 3–10 分钟缓存，
> 生成完立刻 purge 会让 jsDelivr 回源拿到旧表并缓存住。等到 :25 再 purge 就稳了。
> 客户端拿到的表最多比生成时间晚约 1 小时，对开核桃的决策没有影响。

## 开关（不用改代码）

仓库 **Settings → Secrets and variables → Actions → Variables**：

| 变量 | 作用 | 不设置时 |
|---|---|---|
| `TABLE_OFF = true` | **关掉整个定时任务**（跳过 fetch + commit，Actions 里显示 skipped） | 不设置 = 照常每天跑 |
| `TABLE_SCOPE = prime` | 把每天那次降级成 **769 条**（4–5 分钟），`full` 恢复全量 | 不设置 = `full`（3893 条） |

恢复：把 `TABLE_OFF` 删掉（或设成 `false`）、把 `TABLE_SCOPE` 设回 `full`。

> 早期版本用过 `TABLE_ENABLED = false` 当开关，已废弃（脚本不再读它）。如果你的变量列表里还留着一个，
> 顺手删掉即可，不删也不影响。

另一个更省事的关法：Actions → 左侧 `price-table` → 右上 **⋯ → Disable workflow**（GitHub 原生开关，跟我们这套变量是两条独立的路，任选其一）。

手动补跑：Actions → `price-table` → **Run workflow**，可以临时选 `full` 或 `prime`，不影响定时任务的默认值。

## 每小时的负载

| 项 | 数值 |
|---|---|
| 请求数 | 约 4000–5800 次（多数条目 1 次 `/v1/.../statistics` 就够，冷门件再补一次 `/top`） |
| 限流 | 3 请求/秒（wm 官方建议上限），并发 8 |
| 耗时 | 约 25–35 分钟 |
| 平均速率 | 0.05 请求/秒 —— 一天一次，对 wm 几乎无感 |

改周期：`.github/workflows/price-table.yml` 里的 `cron`。
想换成 769 条的小表：把 `--scope full` 去掉（默认就是 prime+安魂+星）。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 大面积 `missing`（> 80 条） | wm 前面有 Cloudflare，可能拦了 GitHub 的出口 IP | 看 Actions 日志里的 `missing` 列表；退回本机跑 `python vl_publish.py`（家用宽带 IP 不会被拦） |
| 出现 429 | 限流撞上了 | 把 `--rate 3` 降到 `--rate 2`，或周期改 2 小时 |
| 客户端拿到旧表 | jsDelivr 对分支引用有缓存（最长约 12 小时） | workflow 每次都会 purge；仍不对就改用 `raw.githubusercontent.com` 地址 |
| Actions 没按时跑 | GitHub 的 cron 是尽力而为，高峰会延迟几分钟 | 对天级表无影响；客户端显示“☁ 20:07 / ☁ 昨 20:07” |
| 任务跑不完（>90 分钟） | 极少见，通常是 wm 大面积 429 | 看日志里的 `missing` 数量；临时把周期放宽或把 `--rate` 降到 2 |

## 文件

| 文件 | 作用 |
|---|---|
| `vl_publish.py` | 抓 769 条 → 生成 `table_v1.json.gz` |
| `vl_cloud.py` | 客户端合并内核 + `table-info` / `table-apply` 命令 |
| `check_table.py` | 跑完检查表是否健康（条目数 / 失败数 / 体积） |
| `.github/workflows/hourly.yml` | 每小时定时任务 |
