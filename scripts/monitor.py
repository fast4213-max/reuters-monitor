"""
Reuters Japan 中東情勢モニター
================================
Google News RSS から jp.reuters.com の中東関連記事を取得し、
送信済みリスト・未通知リストと差分比較して Discord に通知する。

フロー:
  1. Google News RSS を取得・パース（URL は raw のまま保持）
  2. 送信済みリスト（data/sent.json）と未通知リスト（data/pending.json）を読み込み
  3. 新着記事を抽出（過去24時間以内 & 両リストに未登録）
  4. 新着記事を pending.json に追加（古い順）、上限 MAX_PENDING 件でトリム
  5. pending.json の先頭から最大 MAX_NOTIFY 件を Discord へ送信（直前にURL解決）
  6. 送信済み記事を sent.json に記録、pending.json から削除して保存

ファイル役割:
  pending.json … 未送信キュー（送信後は削除される）
  sent.json    … 送信済み記事 ID の記録（重複通知防止、7日分保持）
"""

import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# 定数・設定
# ─────────────────────────────────────────────

JST = timezone(timedelta(hours=9))

# 1回の実行で Discord に送信する最大件数
MAX_NOTIFY = 10

# pending.json の最大保持件数（肥大化防止）
MAX_PENDING = 500

# 新着記事として扱う最大経過時間（時間）
RECENT_HOURS = 24

# sent.json の保持期間（日）：これより古い送信済み記録は自動削除
SENT_RETENTION_DAYS = 7

# 未通知記事を保存するファイルパス
PENDING_FILE = Path("data/pending.json")

# 送信済み記事IDを記録するファイルパス
SENT_FILE = Path("data/sent.json")

# Google News RSS URL
RSS_URL = (
    "https://news.google.com/rss/search"
    "?q=site:jp.reuters.com+"
    "(中東+OR+イスラエル+OR+ガザ+OR+パレスチナ+OR+イラン+OR+レバノン)"
    "&hl=ja&gl=JP&ceid=JP:ja"
)

# Discord Webhook URL（GitHub Secrets から注入）
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ─────────────────────────────────────────────
# ロガー設定
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S JST",
    handlers=[logging.StreamHandler(sys.stdout)],
)

def _to_jst(*args):
    return time.gmtime(time.time() + 9 * 3600)

logging.Formatter.converter = _to_jst
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# RSS 取得・パース
# ─────────────────────────────────────────────

def fetch_rss_articles(session: requests.Session) -> list[dict]:
    """
    Google News RSS を取得し、記事リストを返す。
    URL はリダイレクト解決せず raw のまま返す（解決は送信直前に行う）。

    Returns:
        list[dict]: 各記事は id / title / url（raw） / published_at を持つ。
    """
    logger.info("RSS フィードを取得中: %s", RSS_URL)

    try:
        response = session.get(RSS_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ReutersMonitor/1.0)"
        })
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("RSS 取得タイムアウト（30秒）")
        raise
    except requests.exceptions.RequestException as e:
        logger.exception("RSS 取得失敗: %s", e)
        raise

    articles = []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        logger.exception("XML パース失敗: %s", e)
        raise

    items = root.findall(".//item")
    logger.info("RSS から %d 件取得", len(items))

    for item in items:
        try:
            title        = _get_text(item, "title")
            raw_url      = _get_text(item, "link")
            guid         = _get_text(item, "guid") or raw_url
            pub_date_raw = _get_text(item, "pubDate")
            published_at = _parse_pub_date(pub_date_raw)

            source_node = item.find("source")
            source_url  = (source_node.get("url") or "") if source_node is not None else ""
            source_text = (source_node.text or "").strip() if source_node is not None else ""

            is_reuters = (
                "jp.reuters.com" in source_url
                or "reuters" in source_text.lower()
                or "jp.reuters.com" in raw_url
            )

            if not is_reuters:
                logger.debug("スキップ（Reuters 以外）: source_url=%s title=%s", source_url, title)
                continue

            articles.append({
                "id":           guid,
                "title":        title,
                "url":          raw_url,   # raw URL のまま保持（解決は送信直前）
                "published_at": published_at,
            })

        except Exception as e:
            logger.warning("記事パース中にエラー（スキップ）: %s", e)
            continue

    articles.sort(key=lambda a: a["published_at"])
    logger.info("jp.reuters.com の記事: %d 件", len(articles))
    return articles


def resolve_final_url(session: requests.Session, url: str) -> str:
    """
    リダイレクトを追って最終 URL を返す。
    失敗した場合は元の URL をそのまま返す。
    Discord 送信直前に1件ずつ呼び出す。
    """
    try:
        r = session.get(url, timeout=10, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ReutersMonitor/1.0)"
        })
        return r.url
    except Exception as e:
        logger.warning("URL 解決失敗（元の URL を使用）: %s | %s", url, e)
        return url


def _get_text(element: ET.Element, tag: str) -> str:
    node = element.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _parse_pub_date(pub_date_raw: str) -> str:
    if not pub_date_raw:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        logger.warning("日付パース失敗: %s → 現在時刻を使用", pub_date_raw)
        return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# 送信済みリスト（sent.json）
# ─────────────────────────────────────────────

def load_sent() -> dict[str, str]:
    """
    送信済みリストを読み込む。
    戻り値: {記事ID: 送信日時(ISO)} の辞書。
    ファイルが存在しない場合は空辞書を返す。
    """
    if not SENT_FILE.exists():
        return {}
    try:
        with SENT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("sent.json を読み込み: %d 件", len(data))
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("sent.json 読み込み失敗（空辞書で継続）: %s", e)
        return {}


def save_sent(sent: dict[str, str]) -> None:
    """
    送信済みリストを保存する。
    SENT_RETENTION_DAYS 日より古いエントリは自動削除する。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_RETENTION_DAYS)

    # 古いエントリを削除
    before = len(sent)
    sent = {
        k: v for k, v in sent.items()
        if _parse_iso_safe(v) > cutoff
    }
    removed = before - len(sent)
    if removed:
        logger.info("sent.json: %d 日超えの古いエントリ %d 件を削除", SENT_RETENTION_DAYS, removed)

    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with SENT_FILE.open("w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, indent=2)
        logger.info("sent.json を保存: %d 件", len(sent))
    except OSError as e:
        logger.exception("sent.json 保存失敗: %s", e)
        raise


def _parse_iso_safe(iso_str: str) -> datetime:
    """ISO 8601 文字列を datetime に変換する。失敗時は epoch を返す。"""
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# ─────────────────────────────────────────────
# 未通知リスト（pending.json）の読み書き
# ─────────────────────────────────────────────

def load_pending() -> list[dict]:
    """
    未通知リストを読み込む。
    ファイルが存在しない場合は空リストを返す。
    """
    if not PENDING_FILE.exists():
        logger.info("pending.json が存在しないため、空リストで初期化")
        return []
    try:
        with PENDING_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("pending.json を読み込み: %d 件", len(data))
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("pending.json 読み込み失敗（空リストで継続）: %s", e)
        return []


def save_pending(articles: list[dict]) -> None:
    """
    未通知リストをファイルに保存する。
    MAX_PENDING 件を超える場合は古い順に切り捨てる。
    """
    if len(articles) > MAX_PENDING:
        logger.warning("pending が上限超過（%d件）→ 古い %d 件を破棄", len(articles), len(articles) - MAX_PENDING)
        articles = articles[-MAX_PENDING:]

    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with PENDING_FILE.open("w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        logger.info("pending.json を保存: %d 件", len(articles))
    except OSError as e:
        logger.exception("pending.json 保存失敗: %s", e)
        raise


# ─────────────────────────────────────────────
# 差分検知
# ─────────────────────────────────────────────

def _is_recent(published_at_iso: str, hours: int = RECENT_HOURS) -> bool:
    """published_at が現在時刻から指定時間以内かどうかを返す。"""
    try:
        dt = datetime.fromisoformat(published_at_iso)
        return dt > datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        logger.warning("日付判定失敗（スキップ）: %s", published_at_iso)
        return False


def extract_new_articles(
    fetched: list[dict],
    pending: list[dict],
    sent: dict[str, str],
) -> list[dict]:
    """
    取得した記事のうち、以下をすべて満たすものを返す:
      - 過去 RECENT_HOURS 時間以内に公開
      - pending.json（未送信キュー）に未登録
      - sent.json（送信済み記録）に未登録

    Args:
        fetched: RSS から取得した全記事
        pending: 未送信キュー
        sent:    送信済み記録 {id: 送信日時}

    Returns:
        新着記事リスト（古い順）
    """
    pending_ids = {a["id"] for a in pending}
    sent_ids    = set(sent.keys())
    known_ids   = pending_ids | sent_ids

    new_articles = [
        a for a in fetched
        if a["id"] not in known_ids
        and _is_recent(a["published_at"])
    ]
    logger.info(
        "新着記事（過去%dh以内）: %d 件  ／  既知: pending=%d sent=%d",
        RECENT_HOURS, len(new_articles), len(pending_ids), len(sent_ids)
    )
    return new_articles


# ─────────────────────────────────────────────
# Discord 通知
# ─────────────────────────────────────────────

class RateLimitedError(Exception):
    """Discord 429 レート制限を示す例外。次回実行まで持ち越す。"""


def send_discord_notifications(articles: list[dict], session: requests.Session) -> list[dict]:
    """
    Discord に Embed 形式で通知を送信する。
    429 時は即中断し、残りを次回に持ち越す。

    Returns:
        送信成功した記事のリスト
    """
    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL が設定されていません")
        raise EnvironmentError("DISCORD_WEBHOOK_URL が未設定")

    sent = []
    for i, article in enumerate(articles, start=1):
        logger.info("Discord 通知送信中 (%d/%d): %s", i, len(articles), article["title"])
        try:
            _post_embed(article, session)
            sent.append(article)
        except RateLimitedError:
            logger.warning("レート制限のため送信を中断: 残り %d 件は次回実行時に送信", len(articles) - i + 1)
            break
        except Exception as e:
            logger.error("Discord 送信失敗（スキップ）: %s | 記事: %s", e, article["title"])
            continue

    logger.info("Discord 送信完了: %d / %d 件", len(sent), len(articles))
    return sent


def _post_embed(article: dict, session: requests.Session) -> None:
    """
    1件の記事を Discord Embed 形式で送信する。
    送信直前にリダイレクト URL を解決する。
    429 時は RateLimitedError を送出（待機・リトライなし）。
    """
    # URL解決は送信直前に1件ずつ（fetch時にまとめて解決しない）
    resolved_url      = resolve_final_url(session, article["url"])
    published_display = _format_datetime(article["published_at"])

    payload = {
        "embeds": [
            {
                "title": article["title"][:256],
                "url":   resolved_url,
                "color": 0xE63329,
                "footer": {
                    "text": f"ロイター通信 Japan ｜ {published_display}"
                },
                "fields": [
                    {
                        "name":   "🔗 記事を読む",
                        "value":  resolved_url,
                        "inline": False,
                    }
                ],
            }
        ]
    }

    response = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "不明")
        logger.warning("Discord レート制限: Retry-After=%s 秒 → 残りを次回に持ち越し", retry_after)
        raise RateLimitedError(f"Retry-After={retry_after}")

    response.raise_for_status()


def _format_datetime(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return iso_str


# ─────────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────────

def main() -> None:
    """
    メイン処理フロー:
      1. RSS 取得（URL は raw のまま）
      2. sent.json / pending.json 読み込み
      3. 差分検知（過去24h & 両リスト未登録）→ pending.json に追記
      4. 最大 MAX_NOTIFY 件を Discord 通知（送信直前にURL解決、429時は即中断）
      5. 送信済みを sent.json に記録、pending.json から削除・保存
    """
    logger.info("=== Reuters Japan 中東モニター 開始 ===")

    with requests.Session() as session:
        # ── Step 1: RSS 取得 ──────────────────────
        try:
            fetched_articles = fetch_rss_articles(session)
        except Exception as e:
            logger.critical("RSS 取得に失敗したため処理を中断: %s", e)
            sys.exit(1)

        # ── Step 2: リスト読み込み ─────────────────
        sent    = load_sent()
        pending = load_pending()

        # ── Step 3: 差分検知・追記 ────────────────
        new_articles = extract_new_articles(fetched_articles, pending, sent)

        if new_articles:
            pending.extend(new_articles)
            logger.info("pending 合計: %d 件（新着 %d 件 追加後）", len(pending), len(new_articles))
        else:
            logger.info("新着記事なし")

        # ── Step 4: 最大 MAX_NOTIFY 件を通知 ──────
        if not pending:
            logger.info("未通知なし。処理を終了します。")
            logger.info("=== 処理終了 ===")
            return

        to_notify = pending[:MAX_NOTIFY]
        remaining = pending[MAX_NOTIFY:]

        if remaining:
            logger.info("送信対象: %d 件 ／ 次回繰越: %d 件", len(to_notify), len(remaining))

        sent_articles = send_discord_notifications(to_notify, session)

        # ── Step 5: 送信済みを記録・pending から削除 ──
        now_iso = datetime.now(timezone.utc).isoformat()
        for a in sent_articles:
            sent[a["id"]] = now_iso

        sent_ids           = {a["id"] for a in sent_articles}
        unsent_from_batch  = [a for a in to_notify if a["id"] not in sent_ids]
        updated_pending    = unsent_from_batch + remaining

        save_sent(sent)
        save_pending(updated_pending)

    logger.info(
        "=== 処理完了: 送信=%d件 / pending残=%d件 / sent累計=%d件 ===",
        len(sent_articles), len(updated_pending), len(sent)
    )


if __name__ == "__main__":
    main()
