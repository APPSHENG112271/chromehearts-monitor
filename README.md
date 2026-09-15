# Chrome Hearts 上新监控

盯 [chromehearts.com](https://www.chromehearts.com/) 的官网首页和各分类，
发现**新商品**或**新系列页面**就通过 [ntfy](https://ntfy.sh) 推到手机，点通知直达商品页。

跑在 GitHub Actions 上，不依赖任何本地机器。

## 工作方式

- **每 10 分钟**刷一次首页（新品通常先出现在首页）
- **每 25 分钟**做一次全分类扫描（分类从首页自动发现，新增分类会自动纳入）
- 用商品 SKU 去重，只有没见过的才推送
- 首次运行只建立基线，不推送
- 单次变化超过 8 处时合并成一条通知，防刷屏
- 默认直连；被挡时自动切换到住宅代理（如已配置）

## 文件

| 文件 | 作用 |
|---|---|
| `store_monitor.py` | 监控脚本（纯标准库，无需安装依赖） |
| `.github/workflows/check.yml` | 定时任务 |
| `state/store_seen.json` | 已知商品与页面的基线（脚本自动维护） |

## 需要配置的 Secrets

在 **Settings → Secrets and variables → Actions** 添加：

| 名称 | 说明 | 必填 |
|---|---|---|
| `NTFY_TOPIC` | ntfy 频道名（手机 App 里订阅同名频道即可收通知） | ✅ |
| `NTFY_SERVER` | 自建 ntfy 服务器地址，默认 `https://ntfy.sh` | 可选 |
| `PROXY_URL` | 住宅代理，形如 `http://user:pass@host:port`，直连被挡时兜底 | 可选 |

> ⚠️ ntfy 的频道名相当于密码：知道名字的人既能给你发通知，也能看到你收到的通知。
> 请使用**足够长且随机**的频道名，不要写进代码或 README。

## 本地运行

```bash
export NTFY_TOPIC="你的频道名"
python3 store_monitor.py            # 检查一次
python3 store_monitor.py testpush   # 发条测试通知
```

## 可选开关

| 环境变量 | 作用 |
|---|---|
| `WATCH_BANNER=1` | 首页主视觉换图时也通知（能更早发现 drop 预告，但营销图常换，默认关闭） |
| `STATE_PATH` | 自定义状态文件位置 |
