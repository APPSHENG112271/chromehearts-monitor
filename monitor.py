#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts (Instagram) 新帖监控。
检查目标账号的最新帖子，和上次记录比对，发现新帖就通过 ntfy 推到手机。
只用 Python 标准库，无需 pip install。

抓取走 DataImpulse 住宅代理，并强制美国出口（__cr.us + sticky sessid），
每次运行先确认出口是 US 才去请求 IG，避免异地 IP 触发风控。

环境变量：
  IG_ACCOUNT     目标账号（默认 chromeheartsofficial）
  NTFY_TOPIC     ntfy 频道名（必填，才会推送）
  NTFY_SERVER    ntfy 服务器（默认 https://ntfy.sh）
  IG_SESSIONID   Instagram 登录 cookie 的 sessionid（建议，抓取更稳）
  PROXY_URL      住宅代理，形如 http://user__cr.us:pass@gw.dataimpulse.com:823
  STATE_PATH     状态文件路径（默认 state/last_seen.json）

用法：
  python monitor.py            正常检查一次
  python monitor.py testpush   发一条测试通知，验证 ntfy 配置
"""
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ACCOUNT = os.environ.get("IG_ACCOUNT", "chromeheartsofficial").strip().lstrip("@")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
IG_SESSIONID = os.environ.get("IG_SESSIONID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
STATE_PATH = Path(os.environ.get("STATE_PATH", "state/last_seen.json"))

IG_APP_ID = "936619743392459"
HOSTS = ["www.instagram.com", "i.instagram.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def _proxy_with_session(base, sessid):
    """把 ;sessid.<x> 注入代理用户名，锁定同一个（美国）IP。"""
    if not base:
        return base
    pr = urllib.parse.urlsplit(base)
    user = (pr.username or "").split(";sessid.")[0]
    pwd = pr.password or ""
    host = pr.hostname or ""
    port = (":%d" % pr.port) if pr.port else ""
    user2 = "%s;sessid.%s" % (user, sessid)
    return "%s://%s:%s@%s%s" % (pr.scheme, user2, pwd, host, port)


def _opener_for(proxy_url):
    ctx = ssl.create_default_context()
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


def _country(opener):
    try:
        return opener.open("https://ipinfo.io/country", timeout=15).read().decode().strip()
    except Exception:
        return "?"


def _fetch_ig(opener, host):
    url = "https://%s/api/v1/users/web_profile_info/?username=%s" % (host, ACCOUNT)
    headers = {
        "User-Agent": UA,
        "X-IG-App-ID": IG_APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/%s/" % ACCOUNT,
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    if IG_SESSIONID:
        headers["Cookie"] = "sessionid=%s" % IG_SESSIONID
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_profile():
    """多次尝试：每次拿一个美国 sticky 出口，确认 US 后再请求 IG。"""
    last_err = None
    for attempt in range(6):
        if PROXY_URL:
            sessid = str(random.randint(10 ** 6, 10 ** 9))
            opener = _opener_for(_proxy_with_session(PROXY_URL, sessid))
            country = _country(opener)
            if country != "US":
                print("[retry] 出口国家=%s，换 IP (第 %d 次)" % (country, attempt + 1), file=sys.stderr)
                time.sleep(2)
                continue
            print("[info] 美国出口就绪 sessid=%s" % sessid, file=sys.stderr)
        else:
            opener = _opener_for("")
        for host in HOSTS:
            try:
                return _fetch_ig(opener, host)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in (429, 500, 502, 503, 560):
                    raise
                print("[retry] %s HTTP %s (第 %d 次)" % (host, e.code, attempt + 1), file=sys.stderr)
            except Exception as e:
                last_err = e
                print("[retry] %s %s (第 %d 次)" % (host, e, attempt + 1), file=sys.stderr)
        time.sleep(5 * (attempt + 1) + random.uniform(0, 3))
    if last_err:
        raise last_err
    raise RuntimeError("no attempt reached IG (代理一直不是美国出口?)")


def parse_posts(data):
    user = (data or {}).get("data", {}).get("user")
    if not user:
        return None
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
    posts = []
    for e in edges:
        n = e.get("node", {})
        cap = ""
        cedges = n.get("edge_media_to_caption", {}).get("edges", [])
        if cedges:
            cap = cedges[0].get("node", {}).get("text", "") or ""
        posts.append({
            "shortcode": n.get("shortcode", ""),
            "ts": int(n.get("taken_at_timestamp", 0) or 0),
            "caption": cap,
            "is_video": bool(n.get("is_video")),
        })
    return posts


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def push(title, body, click=None, priority="high", tags="shopping_bags"):
    if not NTFY_TOPIC:
        print("[warn] 未设置 NTFY_TOPIC，跳过推送", file=sys.stderr)
        return
    url = "%s/%s" % (NTFY_SERVER, NTFY_TOPIC)
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)  # ASCII only
    req.add_header("Priority", priority)
    req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print("[push] 已推送: %s" % title)
    except Exception as e:
        print("[error] 推送失败: %s" % e, file=sys.stderr)


def notify_new(post):
    link = "https://www.instagram.com/p/%s/" % post["shortcode"]
    cap = " ".join((post["caption"] or "").split())
    if len(cap) > 200:
        cap = cap[:197] + "..."
    body = cap or ("新视频" if post["is_video"] else "新帖子")
    body = "@%s\n%s\n%s" % (ACCOUNT, body, link)
    title = "New post from @%s" % ACCOUNT  # ASCII
    push(title, body, click=link)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "testpush":
        push("ntfy test - Chrome Hearts monitor",
             "测试成功！以后 @%s 有新帖就会像这样推到你手机。" % ACCOUNT,
             click="https://www.instagram.com/%s/" % ACCOUNT,
             priority="default")
        return

    state = load_state()
    seen_max = int(state.get("max_ts", 0) or 0)

    try:
        data = fetch_profile()
    except urllib.error.HTTPError as e:
        print("[error] 抓取失败 HTTP %s（可能被限流，稍后自动重试）" % e.code, file=sys.stderr)
        return
    except Exception as e:
        print("[error] 抓取失败: %s" % e, file=sys.stderr)
        return

    posts = parse_posts(data)
    if posts is None:
        print("[warn] 拿不到帖子（账号私密 / 被拦 / cookie 失效）", file=sys.stderr)
        return
    if not posts:
        print("[warn] 帖子列表为空。")
        return

    newest_ts = max(p["ts"] for p in posts)

    if not state:
        newest = max(posts, key=lambda p: p["ts"])
        save_state({"max_ts": newest_ts, "last_shortcode": newest["shortcode"],
                    "updated": int(time.time())})
        print("[seed] 已初始化到 %s (ts=%s)，首次不推送。" % (newest["shortcode"], newest_ts))
        return

    new_posts = sorted([p for p in posts if p["ts"] > seen_max], key=lambda p: p["ts"])
    if not new_posts:
        print("[ok] 没有新帖 (max_ts=%s)" % seen_max)
        return

    for p in new_posts:
        print("[NEW] %s ts=%s" % (p["shortcode"], p["ts"]))
        notify_new(p)

    top = new_posts[-1]
    save_state({"max_ts": newest_ts, "last_shortcode": top["shortcode"],
                "updated": int(time.time())})


if __name__ == "__main__":
    main()
