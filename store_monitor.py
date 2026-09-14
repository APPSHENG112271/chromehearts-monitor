#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网上新监控。
抓取各分类页的商品 SKU，和上次记录比对，发现新商品就通过 ntfy 推到手机（带直达购买链接）。
只用 Python 标准库。

优先直连（省代理流量），被挡时自动改走美国住宅代理。

环境变量：
  NTFY_TOPIC     ntfy 频道名（必填，才会推送）
  NTFY_SERVER    ntfy 服务器（默认 https://ntfy.sh）
  PROXY_URL      住宅代理（可选，直连失败时的备用）
  STATE_PATH     状态文件（默认 state/store_seen.json）

用法：
  python store_monitor.py            检查一次
  python store_monitor.py testpush   发测试通知
"""
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.chromehearts.com"
CATEGORIES = ["baccarat", "scents", "boxers-leggings", "intimates", "socks"]

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
STATE_PATH = Path(os.environ.get("STATE_PATH", "state/store_seen.json"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def _proxy_with_session(base, sessid):
    if not base:
        return base
    pr = urllib.parse.urlsplit(base)
    user = (pr.username or "").split(";sessid.")[0]
    pwd = pr.password or ""
    host = pr.hostname or ""
    port = (":%d" % pr.port) if pr.port else ""
    return "%s://%s;sessid.%s:%s@%s%s" % (pr.scheme, user, sessid, pwd, host, port)


def _opener(proxy_url=""):
    ctx = ssl.create_default_context()
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


def _get(url, opener):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with opener.open(req, timeout=40) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_category(cat):
    """先直连，失败再走美国住宅代理。"""
    url = "%s/%s" % (BASE, cat)
    try:
        return _get(url, _opener(""))
    except Exception as e:
        print("[info] %s 直连失败(%s)，改走代理" % (cat, e), file=sys.stderr)
    if not PROXY_URL:
        return None
    for i in range(2):
        try:
            sessid = str(random.randint(10 ** 6, 10 ** 9))
            return _get(url, _opener(_proxy_with_session(PROXY_URL, sessid)))
        except Exception as e:
            print("[retry] %s 代理抓取失败: %s" % (cat, e), file=sys.stderr)
            time.sleep(3)
    return None


def parse_products(html, cat):
    """从分类页 HTML 里抓出 /<cat>/<slug>/<SKU>.html 这种商品链接。"""
    out = {}
    if not html:
        return out
    pat = re.compile(
        r'href=["\']([^"\']*?/%s/([a-z0-9\-]+)/([A-Za-z0-9]+)\.html)["\']' % re.escape(cat),
        re.I)
    for m in pat.finditer(html):
        href, slug, sku = m.group(1), m.group(2), m.group(3)
        if not href.startswith("http"):
            href = BASE + (href if href.startswith("/") else "/" + href)
        name = slug.replace("-", " ").strip().upper()
        out[sku] = {"name": name, "url": href, "cat": cat}
    return out


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
    req = urllib.request.Request("%s/%s" % (NTFY_SERVER, NTFY_TOPIC),
                                 data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)      # ASCII only
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "testpush":
        push("Chrome Hearts store monitor - test",
             "测试成功！以后官网一上新就会这样推给你，点开直达商品页。",
             click=BASE, priority="default")
        return

    state = load_state()
    known = state.get("skus", {})

    current = {}
    ok_cats = 0
    for cat in CATEGORIES:
        html = fetch_category(cat)
        found = parse_products(html, cat)
        if html is not None:
            ok_cats += 1
        print("[info] %s: 抓到 %d 个商品" % (cat, len(found)))
        current.update(found)
        time.sleep(1)

    if ok_cats == 0 or not current:
        print("[error] 所有分类都没抓到商品（可能被挡或页面结构变了），本次不更新状态", file=sys.stderr)
        return

    if not known:
        state = {"skus": current, "updated": int(time.time())}
        save_state(state)
        print("[seed] 已记录 %d 个现有商品，首次不推送。" % len(current))
        return

    new_skus = [s for s in current if s not in known]
    if not new_skus:
        print("[ok] 没有新商品（已知 %d 个）" % len(known))
        return

    for sku in new_skus:
        p = current[sku]
        print("[NEW] %s %s %s" % (sku, p["name"], p["url"]))
        push("Chrome Hearts NEW: %s" % p["cat"],
             "上新了！\n%s\n%s" % (p["name"], p["url"]),
             click=p["url"])

    known.update(current)
    save_state({"skus": known, "updated": int(time.time())})


if __name__ == "__main__":
    main()
