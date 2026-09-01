#!/usr/bin/env python3
"""
把 OPML 里的所有 RSS 源抓下来，合并成一条 feed.xml。

用法:
    python merge_feeds.py                     # 读 feeds.opml，输出 public/feed.xml
    python merge_feeds.py --folder 早报        # 只合并 OPML 里某个文件夹
    python merge_feeds.py --hours 12          # 只要最近 12 小时的
"""

import argparse
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import feedparser

# feedparser 默认不带 UA，有些站会直接拒
feedparser.USER_AGENT = "Mozilla/5.0 (compatible; PersonalFeedMerger/1.0)"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def read_opml(path, folder=None):
    """从 OPML 里取出 (源名称, feed 地址) 列表。folder 非空时只取该文件夹。"""
    root = ET.parse(path).getroot()
    body = root.find("body")
    if body is None:
        return []

    feeds = []

    def walk(node, current_folder):
        for child in node:
            if child.tag != "outline":
                continue
            url = child.get("xmlUrl")
            name = child.get("text") or child.get("title") or url
            if url:
                if folder is None or current_folder == folder:
                    feeds.append((name, url))
            else:
                # 没有 xmlUrl 的 outline 是文件夹
                walk(child, name)

    walk(body, None)
    return feeds


def clean(text, limit=400):
    """去掉 HTML 标签和多余空白，给朗读用的纯文本。"""
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def entry_time(entry):
    """尽量拿到条目时间，拿不到就返回 None。"""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


def fetch(source):
    """抓单个源。任何异常都吞掉，一条源挂掉不能让整个构建失败。"""
    name, url = source
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] {name}: {exc}", file=sys.stderr)
        return []

    if parsed.get("bozo") and not parsed.entries:
        print(f"  [失败] {name}: {parsed.get('bozo_exception')}", file=sys.stderr)
        return []

    items = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = clean(entry.get("title"), limit=200)
        if not link or not title:
            continue
        items.append(
            {
                "source": name,
                "title": title,
                "link": link,
                "summary": clean(entry.get("summary") or entry.get("description")),
                "time": entry_time(entry),
            }
        )
    print(f"  [成功] {name}: {len(items)} 条")
    return items


def build_rss(items, title, self_link):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = self_link or "https://example.com"
    ET.SubElement(channel, "description").text = "由 OPML 合并生成的个人聚合源"
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    for item in items:
        node = ET.SubElement(channel, "item")
        # 标题里带上来源，朗读时能听出是哪家的
        ET.SubElement(node, "title").text = f"[{item['source']}] {item['title']}"
        ET.SubElement(node, "link").text = item["link"]
        ET.SubElement(node, "description").text = item["summary"]
        ET.SubElement(node, "source").text = item["source"]
        guid = ET.SubElement(node, "guid", {"isPermaLink": "true"})
        guid.text = item["link"]
        if item["time"]:
            ET.SubElement(node, "pubDate").text = format_datetime(item["time"])

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--opml", default="feeds.opml", help="OPML 文件路径")
    ap.add_argument("--out", default="public/feed.xml", help="输出路径")
    ap.add_argument("--folder", default=None, help="只合并 OPML 里的某个文件夹")
    ap.add_argument("--hours", type=int, default=48, help="只保留最近 N 小时的条目")
    ap.add_argument("--per-feed", type=int, default=10, help="每个源最多取几条")
    ap.add_argument("--limit", type=int, default=120, help="总共最多输出几条")
    ap.add_argument("--title", default="我的聚合早报")
    ap.add_argument("--self-link", default="", help="发布后的页面地址，可留空")
    args = ap.parse_args()

    sources = read_opml(args.opml, args.folder)
    if not sources:
        print("OPML 里没找到任何源，检查路径或 --folder 名字", file=sys.stderr)
        sys.exit(1)

    print(f"共 {len(sources)} 个源，开始抓取…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, sources))

    # 每个源限流，避免某一家刷屏
    items = []
    for batch in results:
        batch.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        items.extend(batch[: args.per_feed])

    # 时间窗口过滤；没有时间戳的条目保留
    if args.hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        items = [i for i in items if i["time"] is None or i["time"] >= cutoff]

    # 按链接去重
    seen = set()
    deduped = []
    for item in items:
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        deduped.append(item)

    deduped.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    deduped = deduped[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_rss(deduped, args.title, args.self_link))
    print(f"完成：{len(deduped)} 条写入 {out}")


if __name__ == "__main__":
    main()
