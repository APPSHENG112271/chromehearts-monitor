#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网监控。

监控信号：
  1. 新商品   —— 出现没见过的商品 SKU
  2. 到货     —— 之前售罄的商品重新有货（RESTOCK）
  3. 新页面   —— 冒出新的分类/系列页
  4. banner   —— 首页主视觉换图（默认关闭，WATCH_BANNER=1 打开）

分类发现（三路合并，避免漏掉不在首页导航里的分类）：
  a) 首页导航
  b) /shop 页面（含 cgid=XXX 形式，转换成 /xxx 规范地址）
  c) 网站自己的 sitemap

商品链接兼容两种形状：
  /分类/名字/SKU.html   和   /名字/SKU.html

售罄判定：<a class="soldout" href=".../SKU.html">

遵守 robots.txt：不抓 /on/demandware.store/... 这类被禁止的地址，
改用其规范别名（cgid=SWEATPANTS -> /sweatpants）。

可靠性：
  - 抓取失败自动重试；分类页没抓到时沿用该分类商品上次的售罄状态，不误报到货
  - 推送失败的条目不记入状态，下次运行会重新推送
  - 分类页自动翻页（show more），不再只看每个分类的前 12 个商品
  - 状态文件原子写入，并保留 .bak 备份
  - 首页连续抓不到 BLIND_ALERT_AFTER 次会推一条"监控失明"提醒，恢复后再推一条
  - 首页抓不到 / 没抓到商品时退出码为 1

推送范围：只推服装类。baccarat / scents / eyewear / slippers 等非服装分类静默记录，
不推送（MUTE_CATS 可改）。

环境变量：NTFY_TOPIC / NTFY_SERVER / PROXY_URL / WATCH_BANNER / STATE_PATH / MUTE_CATS
用法：python store_monitor.py [run|testpush]
"""
import json
import os
import random
import re
import shutil
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

MAX_PUSH = 8
RETRY_DELAYS = (2, 5)        # 直连失败后的重试间隔（秒），共 1+2 次尝试
RETRY_BUDGET = 200           # 本次运行超过这么多秒后不再重试，避免拖过下一个 5 分钟
PUSH_RETRY_DELAYS = (2, 5, 10)
BLIND_ALERT_AFTER = 6        # 连续这么多次运行抓不到数据就推提醒（5 分钟一次 ≈ 30 分钟）

STATIC_SKIP = {
    "/", "/login", "/cart", "/checkout", "/contact", "/account", "/search",
    "/terms.html", "/privacy.html", "/disclosure.html",
    "/general.html", "/locations.html", "/magazine.html", "/shop",
}

RE_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
RE_IMG = re.compile(r'(?:src|data-src)=["\']([^"\']*demandware\.static[^"\']*)["\']', re.I)
RE_A_TAG = re.compile(r'<a\b[^>]*>', re.I)
RE_CGID = re.compile(r'cgid=([A-Za-z0-9_\-]+)', re.I)
RE_LOC = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>', re.I)
# 商品：1~2 段目录 + SKU.html（SKU 至少 9 位、含数字）
RE_PRODUCT = re.compile(r'^/(?:[A-Za-z0-9\-]+/){1,2}([A-Za-z0-9]{9,})\.html$')
# 分类/页面：1~2 段、不带扩展名
RE_PAGE = re.compile(r'^/[a-z0-9\-]{2,}(?:/[a-z0-9\-]{2,})?$')
RE_CATEGORY = re.compile(r'^/[a-z0-9\-]{2,}$')
# "show more" 按钮里的下一页参数：...Search-UpdateGrid?cgid=xxx&start=12&sz=12
RE_NEXT = re.compile(r'Search-UpdateGrid\?[^"\']*?start=(\d+)(?:&amp;|&)sz=(\d+)', re.I)
MAX_PAGES = 10               # 单个分类最多翻几页，防止死循环

# 只关心服装：这些非服装分类的上新/到货/新页面只记录、不推送。
# 其余（包括没见过的新分类）一律推送，避免漏掉新 drop。
# 可用环境变量 MUTE_CATS（逗号分隔）覆盖；设为空字符串表示全部都推。
MUTE_CATS = {c.strip().lower() for c in
             os.environ.get("MUTE_CATS", "baccarat,scents,eyewear,slippers").split(",") if c.strip()}
MUTE_WORDS = ("PERFUME", "CANDLE", "INCENSE", "SOAP", "MATCHSTICK", "ASHTRAY", "DECANTER",
              "TUMBLER", "GLASS", "FLUTE", "MARTINI", "SLIPPER", "SUNGLASS")

START = time.time()
FAILED = set()   # 本次运行中重试后仍然抓不到的路径（404 不算）


def is_muted(pr):
    """非服装类商品：所在分类在 MUTE_CATS 里，或名称含 MUTE_WORDS 里的词。"""
    if pr.get("cat") in MUTE_CATS:
        return True
    name = (pr.get("name") or "").upper()
    return any(w in name for w in MUTE_WORDS)


def link_muted(path):
    return path.strip("/").split("/")[0] in MUTE_CATS


def sku_from_path(p):
    m = RE_PRODUCT.match(p or "")
    if not m:
        return None
    sku = m.group(1)
    if not re.search(r'\d', sku):
        return None
    return sku


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
    err = None
    for i in range(len(RETRY_DELAYS) + 1):
        try:
            return _get(url, _opener(""))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = "HTTP %s" % e.code
        except Exception as e:
            err = str(e)
        if i < len(RETRY_DELAYS) and time.time() - START < RETRY_BUDGET:
            time.sleep(RETRY_DELAYS[i])
        else:
            break
    print("[info] %s 直连失败(%s)%s" % (path, err, "，改走代理" if PROXY_URL else ""),
          file=sys.stderr)
    if PROXY_URL:
        for _ in range(2):
            try:
                sessid = str(random.randint(10 ** 6, 10 ** 9))
                return _get(url, _opener(_proxy_with_session(PROXY_URL, sessid)))
            except Exception as e:
                print("[retry] %s 代理抓取失败: %s" % (path, e), file=sys.stderr)
                time.sleep(3)
    FAILED.add(path)
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


def cgid_categories(html):
    """把 cgid=SWEATPANTS 这类转成规范地址 /sweatpants（避开 robots 禁止的原始链接）。"""
    out = set()
    for cg in RE_CGID.findall(html or ""):
        slug = cg.strip().lower().replace("_", "-")
        if slug:
            out.add("/" + slug)
    return out


def soldout_skus(html):
    out = set()
    for tag in RE_A_TAG.findall(html or ""):
        if not re.search(r'class=["\'][^"\']*\bsoldout\b', tag, re.I):
            continue
        m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if not m:
            continue
        sku = sku_from_path(_norm_path(m.group(1)) or "")
        if sku:
            out.add(sku)
    return out


def products_from(paths):
    found = {}
    for p in paths:
        sku = sku_from_path(p)
        if not sku:
            continue
        parts = p.strip("/").split("/")
        found[sku] = {
            "cat": parts[0] if len(parts) > 2 else "shop",
            "name": parts[-2].replace("-", " ").upper(),
            "url": BASE + p,
        }
    return found


def sitemap_categories():
    """从网站自己的 sitemap 里取分类页。"""
    cats = set()
    idx = fetch("/sitemap_index.xml")
    maps = []
    if idx:
        maps = [u for u in RE_LOC.findall(idx) if u.lower().endswith(".xml")]
    if not maps:
        maps = [BASE + "/sitemap_0.xml"]
    for mu in maps[:5]:
        p = _norm_path(mu) or "/sitemap_0.xml"
        body = fetch(p)
        if not body:
            continue
        for loc in RE_LOC.findall(body):
            lp = _norm_path(loc)
            if lp and RE_CATEGORY.match(lp) and lp not in STATIC_SKIP:
                cats.add(lp)
    return cats


def images_from(html):
    return set(RE_IMG.findall(html or ""))


def fetch_category(cat):
    """抓取一个分类的所有分页，返回 html 列表；第一页打不开返回 None。

    分类页只带前 12 个商品，其余靠"show more"按钮加载。按钮指向的接口在
    /on/demandware.store/ 下（robots.txt 禁止），改用同样有效的规范地址
    /分类?start=N&sz=M 翻页。后续页抓失败时把该分类记为不完整（FAILED）。
    """
    first = fetch(cat)
    if first is None:
        return None
    pages = [first]
    seen = set()
    html = first
    while len(pages) < MAX_PAGES:
        m = RE_NEXT.search(html)
        if not m:
            break
        start, size = int(m.group(1)), int(m.group(2))
        if start in seen:
            break
        seen.add(start)
        time.sleep(1)
        html = fetch("%s?start=%d&sz=%d" % (cat, start, size))
        if html is None:
            FAILED.add(cat)
            break
        pages.append(html)
    return pages


# ---------- 状态 / 推送 ----------

def _bak_path():
    return STATE_PATH.with_name(STATE_PATH.name + ".bak")


def load_state():
    for p in (STATE_PATH, _bak_path()):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print("[warn] 状态文件 %s 读取失败: %s" % (p, e), file=sys.stderr)
    return {}


def save_state(state):
    """原子写入：先写临时文件再替换；替换前把当前完好的状态备份成 .bak。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            json.loads(STATE_PATH.read_text(encoding="utf-8"))
            shutil.copyfile(STATE_PATH, _bak_path())
        except Exception:
            pass
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def push(title, body, click=None, priority="high", tags="shopping_bags"):
    """推送成功返回 True；失败（重试后仍失败）返回 False。未配置 topic 视为无需推送。"""
    if not NTFY_TOPIC:
        print("[warn] 未设置 NTFY_TOPIC，跳过推送", file=sys.stderr)
        return True
    for i in range(len(PUSH_RETRY_DELAYS) + 1):
        req = urllib.request.Request("%s/%s" % (NTFY_SERVER, NTFY_TOPIC),
                                     data=body.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Priority", priority)
        req.add_header("Tags", tags)
        if click:
            req.add_header("Click", click)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            print("[push] 已推送: %s" % title)
            return True
        except Exception as e:
            print("[error] 推送失败(第 %d 次): %s" % (i + 1, e), file=sys.stderr)
            if i < len(PUSH_RETRY_DELAYS):
                time.sleep(PUSH_RETRY_DELAYS[i])
    return False


# ---------- 主流程 ----------

def blind(state, reason):
    """本次没抓到有效数据：累计连续失败次数，达到阈值推一次提醒。返回退出码 1。"""
    print("[error] %s，本次不更新状态" % reason, file=sys.stderr)
    streak = int(state.get("fail_streak", 0)) + 1
    state["fail_streak"] = streak
    if streak == BLIND_ALERT_AFTER:
        ok = push("Chrome Hearts monitor: blind",
                  "监控已连续 %d 次抓不到官网数据（%s），期间的上新/到货可能漏掉。"
                  % (streak, reason), priority="default", tags="warning")
        if not ok:
            state["fail_streak"] = streak - 1   # 提醒没发出去，下次再试
    save_state(state)
    return 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "testpush":
        ok = push("Chrome Hearts store monitor - test",
                  "测试成功！上新 / 到货都会这样推给你，点开直达商品页。",
                  click=BASE, priority="default")
        return 0 if ok else 1

    state = load_state()
    known_products = state.get("products") or state.get("skus") or {}
    known_links = set(state.get("links") or [])
    links_known = bool(state.get("links"))
    known_images = set(state.get("images") or [])
    prev_streak = int(state.get("fail_streak", 0))

    home = fetch("/")
    if home is None:
        return blind(state, "首页抓不到")

    # --- 分类发现：首页 + /shop + sitemap ---
    cats = {p for p in internal_paths(home) if RE_CATEGORY.match(p) and p not in STATIC_SKIP}
    cats |= cgid_categories(home)

    shop_pages = fetch_category("/shop") or []
    for shop in shop_pages:
        cats |= {p for p in internal_paths(shop) if RE_CATEGORY.match(p) and p not in STATIC_SKIP}
        cats |= cgid_categories(shop)

    cats |= sitemap_categories()
    cats = {c for c in cats if c not in STATIC_SKIP}
    print("[info] 共发现 %d 个分类: %s" % (len(cats), ", ".join(sorted(cats))))

    htmls = [home] + shop_pages
    for cat in sorted(cats):
        pages = fetch_category(cat)
        if pages is None:
            print("[info] %s: 打不开，跳过" % cat)
            continue
        htmls.extend(pages)
        cp = set()
        sold = set()
        for h in pages:
            cp |= internal_paths(h)
            sold |= soldout_skus(h)
        print("[info] %s: 商品 %d 个，售罄 %d 个%s"
              % (cat, len(products_from(cp)), len(sold),
                 "（共 %d 页）" % len(pages) if len(pages) > 1 else ""))
        time.sleep(1)

    all_paths = set()
    soldout_all = set()
    for h in htmls:
        all_paths |= internal_paths(h)
        soldout_all |= soldout_skus(h)

    # 售罄标记只在分类页上可靠；分类页没抓到时，沿用上次的状态，避免误报到货
    failed_pages = {p for p in FAILED if not p.startswith("/sitemap")}

    products = products_from(all_paths)
    for sku, pr in products.items():
        pr["sold_out"] = sku in soldout_all
        prev = known_products.get(sku)
        cat_failed = ("/" + pr["cat"]) in failed_pages or (pr["cat"] == "shop" and bool(failed_pages))
        if prev and cat_failed and "sold_out" in prev:
            pr["sold_out"] = prev["sold_out"]

    page_paths = {p for p in all_paths
                  if RE_PAGE.match(p) and p not in STATIC_SKIP and "/on/" not in p}
    print("[info] 合计商品 %d 个（售罄 %d），页面 %d 个"
          % (len(products), len(soldout_all), len(page_paths)))
    if failed_pages:
        print("[info] 本次有 %d 个页面抓取失败，相关商品沿用上次售罄状态: %s"
              % (len(failed_pages), ", ".join(sorted(failed_pages))))

    if not products:
        return blind(state, "没抓到任何商品")

    # 数据恢复正常：之前发过"失明"提醒的话，告知一声
    if prev_streak >= BLIND_ALERT_AFTER:
        push("Chrome Hearts monitor: recovered",
             "监控已恢复正常（此前连续 %d 次抓不到数据）。" % prev_streak,
             priority="default", tags="white_check_mark")

    images = images_from(home) if WATCH_BANNER else set()

    if not known_products and not links_known:
        save_state({"products": products, "links": sorted(page_paths),
                    "images": sorted(images), "updated": int(time.time()),
                    "fail_streak": 0})
        print("[seed] 已记录 %d 个商品，首次不推送。" % len(products))
        return 0

    new_skus = [k for k in products if k not in known_products]

    restocked = []
    for sku, pr in products.items():
        prev = known_products.get(sku)
        if prev and prev.get("sold_out") is True and pr["sold_out"] is False:
            restocked.append(sku)

    if links_known:
        new_links = [p for p in page_paths if p not in known_links]
    else:
        new_links = []

    # 非服装类（香水、杯具、眼镜、拖鞋等）只记录、不推送
    for sku in new_skus + restocked:
        if is_muted(products[sku]):
            print("[mute] %s %s（不推送）" % (sku, products[sku]["name"]))
    new_skus = [s for s in new_skus if not is_muted(products[s])]
    restocked = [s for s in restocked if not is_muted(products[s])]
    for pp in new_links:
        if link_muted(pp):
            print("[mute] 新页面 %s（不推送）" % pp)
    new_links = [p for p in new_links if not link_muted(p)]

    new_images = []
    if WATCH_BANNER and known_images:
        new_images = [i for i in images if i not in known_images]

    total = len(new_skus) + len(restocked) + len(new_links)

    # 没推送成功的条目不记入状态，下次运行会再推一次
    unsent_new, unsent_restock, unsent_links = set(), set(), set()

    if total == 0:
        print("[ok] 没有变化（已知商品 %d 个，其中售罄 %d）"
              % (len(known_products), len(soldout_all)))
    elif total > MAX_PUSH:
        print("[WARN] 一次出现 %d 处变化，合并成一条推送" % total)
        if not push("Chrome Hearts: %d changes" % total,
                    "官网出现 %d 处变化（新品 %d、到货 %d、新页面 %d）：\n%s"
                    % (total, len(new_skus), len(restocked), len(new_links), BASE),
                    click=BASE):
            unsent_new, unsent_restock, unsent_links = set(new_skus), set(restocked), set(new_links)
    else:
        for sku in new_skus:
            pr = products[sku]
            tag = "（已售罄）" if pr["sold_out"] else ""
            print("[NEW-PRODUCT] %s %s %s" % (sku, pr["name"], pr["url"]))
            if not push("Chrome Hearts NEW: %s" % pr["cat"],
                        "上新了！%s\n%s\n%s" % (tag, pr["name"], pr["url"]), click=pr["url"]):
                unsent_new.add(sku)
        for sku in restocked:
            pr = products[sku]
            print("[RESTOCK] %s %s %s" % (sku, pr["name"], pr["url"]))
            if not push("Chrome Hearts RESTOCK: %s" % pr["cat"],
                        "到货了！之前售罄的这件重新有货：\n%s\n%s" % (pr["name"], pr["url"]),
                        click=pr["url"], tags="rotating_light"):
                unsent_restock.add(sku)
        for pp in sorted(new_links):
            print("[NEW-PAGE] %s" % pp)
            if not push("Chrome Hearts NEW PAGE",
                        "官网出现新页面（可能是新系列/新 drop）：\n%s%s" % (BASE, pp),
                        click=BASE + pp):
                unsent_links.add(pp)

    if unsent_new or unsent_restock or unsent_links:
        print("[warn] 有 %d 条推送失败，不记入状态，下次运行重试"
              % (len(unsent_new) + len(unsent_restock) + len(unsent_links)), file=sys.stderr)

    if new_images:
        push("Chrome Hearts homepage changed",
             "官网首页主视觉换了：\n%s" % BASE, click=BASE, priority="default")

    to_save = dict(products)
    for sku in unsent_new:
        to_save.pop(sku, None)
    for sku in unsent_restock:
        to_save[sku] = known_products[sku]      # 保持"售罄"，下次仍判定为到货
    known_products.update(to_save)
    save_state({"products": known_products,
                "links": sorted((known_links | page_paths) - unsent_links),
                "images": sorted(set(known_images) | images),
                "updated": int(time.time()),
                "fail_streak": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
