#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网监控（首页 + 自动发现的所有分类）。

监控四类信号：
  1. 新商品   —— 出现没见过的商品 SKU
  2. 到货     —— 之前售罄的商品重新有货（RESTOCK）
  3. 新页面   —— 首页/导航里冒出新的分类或系列页（新 drop 常这样出现）
  4. banner   —— 首页主视觉换图（默认关闭，WATCH_BANNER=1 打开）

售罄判定：页面里 <a class="soldout" href="..../SKU.html"> 即表示该 SKU 售罄。

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

MAX_PUSH = 8   # 单次最多推这么多条，超了合并成一条，防刷屏

STATIC_SKIP = {
    "/", "/login", "/cart", "/contact", "/account", "/search",
    "/terms.html", "/privacy.html", "/disclosure.html",
    "/general.html", "/locations.html", "/magazine.html",
}

RE_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
RE_IMG = re.compile(r'(?:src|data-src)=["\']([^"\']*demandware\.static[^"\']*)["\']', re.I)
RE_PRODUCT = re.compile(r'^/[a-z0-9\-]+/[a-z0-9\-]+/([A-Za-z0-9]{6,})\.html$')
RE_CATEGORY = re.compile(r'^/[a-z0-9\-]{2,}$')
RE_PAGE = re.compile(r'^/[a-z0-9\-]{2,}(?:/[a-z0-9\-]{2,})?$')
RE_A_TAG = re.compile(r'<a\b[^>]*>', re.I)


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

def _norm_path(href):
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("http"):
        pr = urllib.parse.urlsplit(href)
        if pr.netloc and pr.netloc.lower() != HOST:
            return None
        path = pr.path
    elif href.startswith("/"):
        path = urllib.parse.urlsplit(href).path
    else:
        return None
    path = re.sub(r"/+$", "", path) or "/"
    return path if path.startswith("/") else None


def internal_paths(html):
    out = set()
    for href in RE_HREF.findall(html or ""):
        p = _norm_path(href)
        if p:
            out.add(p)
    return out


def soldout_skus(html):
    """页面里 <a class="soldout" href=".../SKU.html"> 对应的 SKU 集合。"""
    out = set()
    for tag in RE_A_TAG.findall(html or ""):
        if not re.search(r'class=["\'][^"\']*\bsoldout\b', tag, re.I):
            continue
        m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if not m:
            continue
        p = _norm_path(m.group(1))
        if not p:
            continue
        pm = RE_PRODUCT.match(p)
        if pm:
            out.add(pm.group(1))
    return out


def products_from(paths):
    found = {}
    for p in paths:
        m = RE_PRODUCT.match(p)
        if not m:
            continue
        parts = p.strip("/").split("/")
        found[m.group(1)] = {
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
             "测试成功！上新 / 到货都会这样推给你，点开直达商品页。",
             click=BASE, priority="default")
        return

    state = load_state()
    known_products = state.get("products") or state.get("skus") or {}
    known_links = set(state.get("links") or [])
    links_known = bool(state.get("links"))
    known_images = set(state.get("images") or [])

    home = fetch("/")
    if home is None:
        print("[error] 首页抓不到，本次不更新状态", file=sys.stderr)
        return

    htmls = [home]
    home_paths = internal_paths(home)
    cats = categories_from(home_paths)
    print("[info] 首页发现 %d 个分类: %s" % (len(cats), ", ".join(sorted(cats))))

    for cat in sorted(cats):
        html = fetch(cat)
        if html is None:
            continue
        htmls.append(html)
        cp = internal_paths(html)
        print("[info] %s: 商品 %d 个，售罄 %d 个"
              % (cat, len(products_from(cp)), len(soldout_skus(html))))
        time.sleep(1)

    all_paths = set()
    soldout_all = set()
    for h in htmls:
        all_paths |= internal_paths(h)
        soldout_all |= soldout_skus(h)

    products = products_from(all_paths)
    for sku in products:
        products[sku]["sold_out"] = sku in soldout_all

    page_paths = {p for p in all_paths
                  if RE_PAGE.match(p) and p not in STATIC_SKIP and "/on/" not in p}
    print("[info] 合计商品 %d 个（售罄 %d），页面 %d 个"
          % (len(products), len(soldout_all), len(page_paths)))

    if not products:
        print("[error] 没抓到任何商品（可能被挡或结构变了），本次不更新状态", file=sys.stderr)
        return

    images = images_from(home) if WATCH_BANNER else set()

    # 首次运行：只记录，不推送
    if not known_products and not links_known:
        save_state({"products": products, "links": sorted(page_paths),
                    "images": sorted(images), "updated": int(time.time())})
        print("[seed] 已记录 %d 个商品（售罄 %d）、%d 个页面，首次不推送。"
              % (len(products), len(soldout_all), len(page_paths)))
        return

    # 1) 新商品
    new_skus = [k for k in products if k not in known_products]

    # 2) 到货：之前明确售罄，现在有货
    restocked = []
    for sku, pr in products.items():
        prev = known_products.get(sku)
        if not prev:
            continue
        if prev.get("sold_out") is True and pr["sold_out"] is False:
            restocked.append(sku)

    # 3) 新页面
    if links_known:
        new_links = [p for p in page_paths if p not in known_links]
    else:
        new_links = []
        print("[info] 首次记录页面清单，本次不推送新页面")

    # 4) banner
    new_images = []
    if WATCH_BANNER and known_images:
        new_images = [i for i in images if i not in known_images]

    total = len(new_skus) + len(restocked) + len(new_links)

    if total == 0:
        print("[ok] 没有变化（已知商品 %d 个，其中售罄 %d）"
              % (len(known_products), len(soldout_all)))
    elif total > MAX_PUSH:
        print("[WARN] 一次出现 %d 处变化，合并成一条推送" % total)
        push("Chrome Hearts: %d changes" % total,
             "官网出现 %d 处变化（新品 %d、到货 %d、新页面 %d）：\n%s"
             % (total, len(new_skus), len(restocked), len(new_links), BASE),
             click=BASE)
    else:
        for sku in new_skus:
            pr = products[sku]
            tag = "（售罄）" if pr["sold_out"] else ""
            print("[NEW-PRODUCT] %s %s %s" % (sku, pr["name"], pr["url"]))
            push("Chrome Hearts NEW: %s" % pr["cat"],
                 "上新了！%s\n%s\n%s" % (tag, pr["name"], pr["url"]), click=pr["url"])
        for sku in restocked:
            pr = products[sku]
            print("[RESTOCK] %s %s %s" % (sku, pr["name"], pr["url"]))
            push("Chrome Hearts RESTOCK: %s" % pr["cat"],
                 "到货了！之前售罄的这件重新有货：\n%s\n%s" % (pr["name"], pr["url"]),
                 click=pr["url"], tags="rotating_light")
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
