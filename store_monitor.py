#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网上新监控（首页 + 自动发现的所有分类）。

监控三类信号：
  1. 新商品   —— 任何页面上出现没见过的商品 SKU
  2. 新页面   —— 首页/导航里冒出新的分类或系列页（新 drop 常这样出现）
  3. banner   —— 首页主视觉换图（默认关闭，WATCH_BANNER=1 打开）

优先直连（省代理流量），被挡时自动改走美国住宅代理。只用 Python 标准库。

环境变量：
  NTFY_TOPIC     ntfy 频道名（必填，才会推送）
  NTFY_SERVER    ntfy 服务器（默认 https://ntfy.sh）
  PROXY_URL      住宅代理（可选，直连失败时备用）
  WATCH_BANNER   设为 1 时，首页图片变化也通知
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
HOST = "www.chromehearts.com"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
WATCH_BANNER = os.environ.get("WATCH_BANNER", "").strip() == "1"
STATE_PATH = Path(os.environ.get("STATE_PATH", "state/store_seen.json"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# 这些是固定页面，不当作"新页面"信号
STATIC_SKIP = {
    "/", "/login", "/cart", "/contact", "/account", "/search",
    "/terms.html", "/privacy.html", "/disclosure.html",
    "/general.html", "/locations.html", "/magazine.html",
}

RE_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
RE_IMG = re.compile(r'(?:src|data-src)=["\']([^"\']*demandware\.static[^"\']*)["\']', re.I)
RE_PRODUCT = re.compile(r'^/[a-z0-9\-]+/[a-z0-9\-]+/([A-Za-z0-9]{6,})\.html$')
RE_CATEGORY = re.compile(r'^/[a-z0-9\-]{2,}$')
# 只有这种形状才算"页面"：/collection 或 /collection/sub，不带扩展名
RE_PAGE = re.compile(r'^/[a-z0-9\-]{2,}(?:/[a-z0-9\-]{2,})?$')
MAX_PUSH = 8   # 单次运行最多推这么多条，超了就合并成一条，防刷屏


# ---------- 抓取 ----------

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


def fetch(path):
    """先直连，失败再走美国住宅代理。"""
    url = BASE + path if path.startswith("/") else path
    try:
        return _get(url, _opener(""))
    except Exception as e:
        print("[info] %s 直连失败(%s)，改走代理" % (path, e), file=sys.stderr)
    if not PROXY_URL:
        return None
    for _ in range(2):
        try:
            sessid = str(random.randint(10 ** 6, 10 ** 9))
            return _get(url, _opener(_proxy_with_session(PROXY_URL, sessid)))
        except Exception as e:
            print("[retry] %s 代理抓取失败: %s" % (path, e), file=sys.stderr)
            time.sleep(3)
    return None


# ---------- 解析 ----------

def internal_paths(html):
    """页面里所有指向本站的链接，规范化成 /path（去掉参数和锚点）。"""
    out = set()
    if not html:
        return out
    for href in RE_HREF.findall(html):
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("http"):
            pr = urllib.parse.urlsplit(href)
            if pr.netloc and pr.netloc.lower() != HOST:
                continue
            path = pr.path
        elif href.startswith("/"):
            path = urllib.parse.urlsplit(href).path
        else:
            continue
        path = re.sub(r"/+$", "", path) or "/"
        if path and path.startswith("/"):
            out.add(path)
    return out


def products_from(paths):
    """从路径集合里挑出商品页，返回 {sku: {name, url, cat}}。"""
    found = {}
    for p in paths:
        m = RE_PRODUCT.match(p)
        if not m:
            continue
        sku = m.group(1)
        parts = p.strip("/").split("/")
        found[sku] = {
            "cat": parts[0],
            "name": parts[1].replace("-", " ").upper(),
            "url": BASE + p,
        }
    return found


def categories_from(paths):
    return {p for p in paths if RE_CATEGORY.match(p) and p not in STATIC_SKIP}


def images_from(html):
    return set(RE_IMG.findall(html or ""))


# ---------- 状态 / 推送 ----------

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


# ---------- 主流程 ----------

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "testpush":
        push("Chrome Hearts store monitor - test",
             "测试成功！官网首页或分类一上新，就会这样推给你，点开直达。",
             click=BASE, priority="default")
        return

    state = load_state()
    known_products = state.get("products") or state.get("skus") or {}
    known_links = set(state.get("links") or [])
    links_known = bool(state.get("links"))   # 旧版状态没有 links，首次记录时不推送
    known_images = set(state.get("images") or [])

    home = fetch("/")
    if home is None:
        print("[error] 首页抓不到，本次不更新状态", file=sys.stderr)
        return

    home_paths = internal_paths(home)
    cats = categories_from(home_paths)
    print("[info] 首页发现 %d 个分类: %s" % (len(cats), ", ".join(sorted(cats))))

    all_paths = set(home_paths)
    pages_ok = 1
    for cat in sorted(cats):
        html = fetch(cat)
        if html is None:
            continue
        pages_ok += 1
        paths = internal_paths(html)
        all_paths |= paths
        print("[info] %s: 抓到 %d 个商品" % (cat, len(products_from(paths))))
        time.sleep(1)

    products = products_from(all_paths)
    # 只把"真页面"当作链接信号，滤掉 css/js/图片等静态资源
    page_paths = {p for p in all_paths
                  if RE_PAGE.match(p) and p not in STATIC_SKIP and "/on/" not in p}
    print("[info] 合计商品 %d 个，页面 %d 个" % (len(products), len(page_paths)))

    if pages_ok == 0 or not products:
        print("[error] 没抓到任何商品（可能被挡或结构变了），本次不更新状态", file=sys.stderr)
        return

    images = images_from(home) if WATCH_BANNER else set()

    # 首次运行：只记录，不推送
    if not known_products and not known_links:
        save_state({"products": products, "links": sorted(page_paths),
                    "images": sorted(images), "updated": int(time.time())})
        print("[seed] 已记录 %d 个商品、%d 个页面，首次不推送。" % (len(products), len(page_paths)))
        return

    # 先算出所有变化，再决定怎么推（防刷屏）
    new_skus = [k for k in products if k not in known_products]

    if links_known:
        new_links = [p for p in page_paths if p not in known_links]
    else:
        new_links = []
        print("[info] 首次记录页面清单，本次不推送新页面")

    new_images = []
    if WATCH_BANNER and known_images:
        new_images = [i for i in images if i not in known_images]

    total = len(new_skus) + len(new_links)

    if total == 0:
        print("[ok] 没有变化（已知商品 %d 个）" % len(known_products))
    elif total > MAX_PUSH:
        # 变化太多（多半是网站改版），只发一条汇总，不刷屏
        print("[WARN] 一次出现 %d 处变化，合并成一条推送" % total)
        push("Chrome Hearts: %d changes" % total,
             "官网出现 %d 处变化（新商品 %d、新页面 %d），可能是改版或大批上新：\n%s"
             % (total, len(new_skus), len(new_links), BASE),
             click=BASE)
    else:
        for sku in new_skus:
            pr = products[sku]
            print("[NEW-PRODUCT] %s %s %s" % (sku, pr["name"], pr["url"]))
            push("Chrome Hearts NEW: %s" % pr["cat"],
                 "上新了！\n%s\n%s" % (pr["name"], pr["url"]), click=pr["url"])
        for pp in sorted(new_links):
            print("[NEW-PAGE] %s" % pp)
            push("Chrome Hearts NEW PAGE",
                 "官网出现新页面（可能是新系列/新 drop）：\n%s%s" % (BASE, pp),
                 click=BASE + pp)

    if new_images:
        print("[NEW-BANNER] %d 张新图" % len(new_images))
        push("Chrome Hearts homepage changed",
             "官网首页主视觉换了，可能有新动作：\n%s" % BASE,
             click=BASE, priority="default")

    known_products.update(products)
    save_state({"products": known_products,
                "links": sorted(set(known_links) | page_paths),
                "images": sorted(set(known_images) | images),
                "updated": int(time.time())})


if __name__ == "__main__":
    main()
