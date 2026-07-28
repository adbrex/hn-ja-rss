#!/usr/bin/env python3
"""
Hacker News の人気記事を取得し、タイトルだけ日本語訳して RSS を生成する。
 
  python build.py             通常実行
  python build.py --dry-run   翻訳APIを呼ばず、取得結果だけ表示
  python build.py --force     キャッシュを無視して全件翻訳し直す
 
環境変数 ANTHROPIC_API_KEY が必要（--dry-run 時は不要）。
"""
 
from __future__ import annotations
 
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
 
# ===========================================================================
# 設定 — ここだけ触れば挙動を変えられます
# ===========================================================================
 
CONFIG = {
    # 取得元: Algolia が運営する Hacker News 公式検索API。
    # tags=front_page = いまフロントページに載っている記事（点数つきで返る）。
    #   もっと広く漁りたい場合の例:
    #     過去24時間で人気: "...search?tags=story&numericFilters=created_at_i>UNIXTIME"
    "source_url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50",
    # フィードに載せる最大件数（人気順の上位から）
    "max_items": 30,
    # この点数未満は切り捨てる。0 なら足切りなし
    "min_points": 0,
    # タイトル先頭に [520pt] のように点数を付ける
    "show_points_in_title": True,
    # 翻訳モデル。タイトル程度なら haiku で十分（$1 / $5 per 1M tokens）
    "model": "claude-haiku-4-5",
    # 1回のAPIリクエストにまとめるタイトル数
    "batch_size": 40,
    # 出力先
    "output_path": "docs/hn-ja.xml",
    "cache_path": "state/translations.json",
    # 翻訳キャッシュの保持期間（日）。これより古い未使用エントリは捨てる
    "cache_ttl_days": 120,
    # フィードのメタ情報
    "feed_title": "Hacker News 人気記事（日本語タイトル）",
    "feed_link": "https://news.ycombinator.com/",
    "feed_description": (
        "Hacker News のフロントページから人気順に最大30件を抽出し、"
        "タイトルを日本語訳したフィードです。1日3回更新。"
    ),
}
 
SYSTEM_PROMPT = """あなたは技術ニュースの見出しを日本語に訳す翻訳者です。
 
ルール:
- 出力は日本語の見出しのみ。説明・注釈・補足は一切付けない。
- 製品名・企業名・技術用語（PostgreSQL, Rust, Kubernetes, GitHub, LLM など）は
  原語のまま残す。無理にカタカナや訳語にしない。
- "Show HN:" "Ask HN:" "Tell HN:" などの接頭辞はそのまま残す。
- 見出しらしく簡潔に。体言止めを基本とする。
- 原文にない情報を足さない。訳せない固有名詞はそのまま残す。
 
入力は {"1": "英語タイトル", "2": "英語タイトル", ...} 形式のJSONです。
同じキーで日本語訳を返してください。
JSONオブジェクトのみを出力し、コードフェンスや前置きは付けないこと。"""
 
 
# ===========================================================================
# 取得
# ===========================================================================
 
 
def fetch_stories(url: str, retries: int = 3) -> list[dict]:
    """HN検索APIを叩いて記事一覧を取得する。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "hn-ja-rss/1.0 (personal RSS translator)"}
    )
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(2**attempt)
    else:
        raise RuntimeError(f"記事の取得に失敗しました: {last_err}")
 
    stories = []
    for hit in payload.get("hits", []):
        title = (hit.get("title") or "").strip()
        story_id = hit.get("objectID")
        if not title or not story_id:
            continue
        hn_url = f"https://news.ycombinator.com/item?id={story_id}"
        stories.append(
            {
                "id": story_id,
                "title_en": title,
                # Ask HN / Show HN の自己投稿には外部URLが無い→HNのページを指す
                "link": hit.get("url") or hn_url,
                "hn_url": hn_url,
                "points": int(hit.get("points") or 0),
                "num_comments": int(hit.get("num_comments") or 0),
                "author": hit.get("author") or "",
                "created_at": hit.get("created_at") or "",
            }
        )
    return stories
 
 
def select_top(stories: list[dict], cfg: dict) -> list[dict]:
    """人気順に並べ替えて上位N件を返す。"""
    filtered = [s for s in stories if s["points"] >= cfg["min_points"]]
    filtered.sort(key=lambda s: (s["points"], s["num_comments"]), reverse=True)
    return filtered[: cfg["max_items"]]
 
 
# ===========================================================================
# 翻訳
# ===========================================================================
 
 
def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        print(f"  ! キャッシュを読めなかったので作り直します: {err}", file=sys.stderr)
        return {}
 
 
def save_cache(path: Path, cache: dict, ttl_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    pruned = {k: v for k, v in cache.items() if v.get("ts", "") >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    dropped = len(cache) - len(pruned)
    if dropped:
        print(f"  キャッシュから古い {dropped} 件を削除")
 
 
def _extract_json(text: str) -> dict:
    """モデル出力からJSONオブジェクトを取り出す（コードフェンス対策込み）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSONが見つかりません")
    return json.loads(text[start : end + 1])
 
 
def translate_batch(client, titles: list[str], model: str) -> dict[str, str]:
    """英語タイトルのリストを日本語に訳して {英語: 日本語} を返す。"""
    numbered = {str(i): t for i, t in enumerate(titles, start=1)}
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(numbered, ensure_ascii=False, indent=1),
            }
        ],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    parsed = _extract_json(raw)
 
    usage = resp.usage
    print(
        f"  翻訳 {len(titles)}件 "
        f"(in {usage.input_tokens} / out {usage.output_tokens} tokens)"
    )
 
    out = {}
    for key, original in numbered.items():
        translated = parsed.get(key)
        if isinstance(translated, str) and translated.strip():
            out[original] = translated.strip()
        else:
            print(f"  ! 訳が欠落したため原文のまま: {original[:60]}", file=sys.stderr)
            out[original] = original
    return out
 
 
def translate_all(stories: list[dict], cfg: dict, cache: dict, force: bool) -> None:
    """未翻訳のタイトルだけAPIに投げ、各storyに title_ja を埋める。"""
    todo = []
    for story in stories:
        hit = None if force else cache.get(story["title_en"])
        if hit:
            story["title_ja"] = hit["ja"]
            hit["ts"] = datetime.now(timezone.utc).isoformat()  # 生存期間を延ばす
        else:
            todo.append(story["title_en"])
 
    print(f"  キャッシュ命中 {len(stories) - len(todo)} 件 / 新規 {len(todo)} 件")
    if not todo:
        return
 
    from anthropic import Anthropic  # 遅延import: --dry-run では不要
 
    client = Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む
    results = {}
    for i in range(0, len(todo), cfg["batch_size"]):
        chunk = todo[i : i + cfg["batch_size"]]
        try:
            results.update(translate_batch(client, chunk, cfg["model"]))
        except Exception as err:  # 翻訳失敗でフィード生成全体を止めない
            print(f"  ! 翻訳に失敗、原文のまま出力します: {err}", file=sys.stderr)
            results.update({t: t for t in chunk})
 
    now = datetime.now(timezone.utc).isoformat()
    for story in stories:
        if "title_ja" not in story:
            ja = results.get(story["title_en"], story["title_en"])
            story["title_ja"] = ja
            if ja != story["title_en"]:  # 失敗した訳はキャッシュしない
                cache[story["title_en"]] = {"ja": ja, "ts": now}
 
 
# ===========================================================================
# RSS生成
# ===========================================================================
 
 
def _pubdate(iso: str) -> str:
    """ISO8601 を RSS の RFC822 形式に変換する。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)
 
 
def build_rss(stories: list[dict], cfg: dict, feed_url: str) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{escape(cfg['feed_title'])}</title>",
        f"    <link>{escape(cfg['feed_link'])}</link>",
        f"    <description>{escape(cfg['feed_description'])}</description>",
        "    <language>ja</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        "    <ttl>240</ttl>",
    ]
    if feed_url:
        out.append(
            f'    <atom:link href="{escape(feed_url)}" rel="self" '
            'type="application/rss+xml"/>'
        )
 
    for story in stories:
        title = story["title_ja"]
        if cfg["show_points_in_title"]:
            title = f"[{story['points']}pt] {title}"
 
        body = (
            f"<p>{escape(story['title_en'])}</p>"
            f"<p>{story['points']} points / {story['num_comments']} comments"
            f" — by {escape(story['author'])}</p>"
            f'<p><a href="{escape(story["hn_url"])}">HNのコメントを読む</a></p>'
        )
 
        out += [
            "    <item>",
            f"      <title>{escape(title)}</title>",
            f"      <link>{escape(story['link'])}</link>",
            f'      <guid isPermaLink="false">hn-{story["id"]}</guid>',
            f"      <pubDate>{_pubdate(story['created_at'])}</pubDate>",
            f"      <comments>{escape(story['hn_url'])}</comments>",
            f"      <description><![CDATA[{body}]]></description>",
            "    </item>",
        ]
 
    out += ["  </channel>", "</rss>", ""]
    return "\n".join(out)
 
 
# ===========================================================================
 
 
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="翻訳APIを呼ばず取得結果だけ表示"
    )
    parser.add_argument(
        "--force", action="store_true", help="キャッシュを無視して全件訳し直す"
    )
    args = parser.parse_args()
 
    cfg = CONFIG
    root = Path(__file__).parent
    feed_url = os.environ.get("FEED_URL", "")
 
    print("Hacker News を取得中...")
    stories = fetch_stories(cfg["source_url"])
    print(f"  {len(stories)} 件取得")
 
    stories = select_top(stories, cfg)
    if not stories:
        print("対象記事がありませんでした。既存のフィードは残します。")
        return 0
    print(
        f"  人気順に上位 {len(stories)} 件を採用 "
        f"({stories[-1]['points']}pt 〜 {stories[0]['points']}pt)"
    )
 
    if args.dry_run:
        for i, s in enumerate(stories, 1):
            print(f"  {i:2d}. [{s['points']:4d}pt] {s['title_en']}")
        return 0
 
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("エラー: 環境変数 ANTHROPIC_API_KEY が未設定です。", file=sys.stderr)
        return 1
 
    cache_path = root / cfg["cache_path"]
    cache = load_cache(cache_path)
    print("タイトルを翻訳中...")
    translate_all(stories, cfg, cache, args.force)
    save_cache(cache_path, cache, cfg["cache_ttl_days"])
 
    out_path = root / cfg["output_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_rss(stories, cfg, feed_url), encoding="utf-8")
    print(f"生成完了: {out_path} ({len(stories)} 件)")
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 
