# Chrome Hearts Instagram 新帖监控

跑在 **GitHub Actions**（GitHub 的服务器）上，每 30 分钟检查一次 [@chromeheartsofficial](https://www.instagram.com/chromeheartsofficial/)，
发现新帖就通过 **ntfy** 推到你手机。不依赖你的电脑开不开机。

- `monitor.py` — 监控脚本（纯 Python 标准库，无需安装依赖）
- `.github/workflows/check.yml` — 定时任务（每 30 分钟 + 手动按钮）
- `state/last_seen.json` — 自动记录上次看到的帖子（脚本自己写，不用管）

---

## 一、手机装 ntfy（2 分钟）

1. 手机应用商店搜 **ntfy**，安装（iOS / Android 都有，免费，不用注册）。
2. 打开 App → 点 **+** 订阅一个频道。频道名要**长、随机、别人猜不到**，比如：

   ```
   appsheng-112271-ypen
   ```

   （任何人知道频道名都能往里发消息，所以别用简单的名字。把你自己起的记下来，下一步要用。）

## 二、建 GitHub 仓库并上传代码

需要一个 GitHub 账号。二选一：

### 方式 A：命令行（你有 gh / git 的话，最快）

在这个文件夹里打开终端，依次运行（把 `你的用户名` 换掉）：

```bash
cd ~/Documents/chromehearts-monitor
git init && git add -A && git commit -m "init chrome hearts monitor"
gh repo create chromehearts-monitor --private --source=. --push
```

没装 `gh` 的话，先 `brew install gh` 再 `gh auth login`。

### 方式 B：网页上传（不想用命令行）

1. 打开 github.com → 右上 **+** → **New repository** → 名字随便（建议设 **Private**）→ Create。
2. 进仓库 → **Add file → Upload files** → 把这个文件夹里的东西拖进去。
   - ⚠️ 网页上传对 `.github/workflows/` 这种嵌套文件夹不友好。**强烈建议用方式 A**；
     如果一定要网页传，先手动 **Add file → Create new file**，文件名直接填
     `.github/workflows/check.yml`（GitHub 会自动帮你建目录），再把内容粘进去。

## 三、填 Secrets（告诉脚本往哪推）

仓库页 → **Settings → Secrets and variables → Actions → New repository secret**，加这几个：

| 名称 | 值 | 必填 |
|---|---|---|
| `NTFY_TOPIC` | 第一步你起的频道名，如 `appsheng-112271-ypen` | ✅ 必填 |
| `IG_SESSIONID` | Instagram 登录 cookie（见第四步）| 强烈建议 |
| `NTFY_SERVER` | 一般不用填，默认 `https://ntfy.sh` | 可选 |

## 四、取 IG_SESSIONID（强烈建议，不然容易被拦）

GitHub 服务器用的是"数据中心 IP"，Instagram 对它很敏感，不带登录 cookie 经常会返回
401 拿不到数据。带上一个登录状态就稳很多。

> ⚠️ **建议用一个 Instagram 小号 / 备用号**来取这个 cookie，别用你主号——
> 自动化访问偶尔会让账号及出事会要求验证。小号即使被限也不心疼。

取法（电脑浏览器）：

1. 用小号登录 instagram.com。
2. 按 **F12** 打开开发者工具 → **Application**（应用）标签 → 左侧 **Cookies** → 点 `https://www.instagram.com`。
3. 找到名为 **`sessionid`** 的那一行，复制它的 **Value**（一长串，形如 `7358...%3A...%3A...`）。
4. 把这串粘到 GitHub 的 `IG_SESSIONID` secret 里。

（cookie 过期后推送会停，重新取一次、更新 secret 即可。）

## 五、开启并测试

1. 仓库页 → **Actions** 标签 → 如果提示，点 **I understand… enable**。
2. 左侧选 **chromehearts-monitor** → 右边 **Run workflow** 手动跑一次。
   - 第一次运行是**记录当前状态**，不会推送（否则会把现有的帖子全推一遍）。
   - 之后每 30 分钟自动跑；**只有出现比这次更新的帖子才会推你手机**。
3. 想立刻验证 ntfy 通不通？在本地或 Actions 里跑 `python monitor.py testpush`，手机应该马上收到一条测试通知。

---

## 本地先试一下（可选，在你自己的 Mac 终端里）

你的真实网络能连 IG，先本地验证脚本能抓到数据、ntfy 能收到：

```bash
cd ~/Documents/chromehearts-monitor
export NTFY_TOPIC="appsheng-112271-ypen"     # 换成你的频道名
export IG_SESSIONID="粘贴你的sessionid"           # 可选
python3 monitor.py testpush     # 手机应收到测试通知
python3 monitor.py              # 首次会 seed；再跑一次应显示"没有新帖"
```

如果这一步 `python3 monitor.py` 报 `抓取失败 HTTP 401`，说昍你的网络也需要登录 cookie，
补上 `IG_SESSIONID` 再试。如果连超时，可能是你在公司 VPN 后面挡了 IG。

---

## 说明与小坑

- **检查频率**：`*/30`（每 30 分钟）。想更勤可以改 `.github/workflows/check.yml` 里的 cron，
  但 GitHub 免费升额足够、且太频繁也更容易被 IG 限流，30 分钟是个稳妥值。- **GitHub 会休眠定时任务**：如果仓库 **60 天没有任何提交活动**，GitHub 会自动停用定时工作流。
  本脚本每次有新帖都会自动提交状态文件，正常有更新就不会休眠；长期没更新的话偶尔手动点一下
  Run workflow 即可。- **不会漏也不会重**：靠帖子的发布时间戳判断，置顶的老帖不会误报，同一条帖也不会重复推。- **私密/被拦**：日志里出现"拿不到帖子"多半是缺 `IG_SESSIONID` 或 cookie 过期，按第四步更新。
