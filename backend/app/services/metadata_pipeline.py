"""Metadata lookup and edition matching.

The pipeline deliberately selects one source record as an edition.  It never
builds a synthetic edition by filling missing fields from unrelated results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.config import settings

GOOGLE_BOOKS_API_KEY = settings.GOOGLE_BOOKS_API_KEY
# Use Uvicorn's configured handler so INFO progress events are visible when the
# pipeline runs inside FastAPI. It still falls back to normal logging in tests.
LOGGER = logging.getLogger("uvicorn.error.metadata_pipeline")
LOGGER.setLevel(
    getattr(logging, os.getenv("METADATA_LOG_LEVEL", "INFO").upper(), logging.INFO)
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RESULTS_PER_SOURCE = 5
CACHE_TTL_SECONDS = 60 * 60 * 6
GOOGLE_BOOKS_COOLDOWN_SECONDS = 60.0
BOOKS_COOLDOWN_SECONDS = 60.0
READMOO_COOLDOWN_SECONDS = 5 * 60.0
OPEN_LIBRARY_COOLDOWN_SECONDS = 60.0

_shared_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()
_google_books_lock = asyncio.Lock()
_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_inflight: dict[tuple[str, ...], asyncio.Task[dict[str, Any]]] = {}
_inflight_lock = asyncio.Lock()
_source_cooldowns: dict[str, float] = {}
_source_failures: dict[str, int] = {}


def source_is_paused(source: str) -> bool:
    return _source_cooldowns.get(source, 0.0) > time.monotonic()


def log_paused_source(source: str) -> None:
    remaining = max(_source_cooldowns.get(source, 0.0) - time.monotonic(), 0.0)
    LOGGER.info(
        "metadata_source_skipped source=%s reason=circuit_open remaining=%.1fs",
        source,
        remaining,
        extra={
            "event": "metadata_source_skipped",
            "source": source,
            "reason": "circuit_open",
            "remaining_seconds": round(remaining, 1),
        },
    )


def pause_source(
    source: str,
    *,
    reason: str,
    base_seconds: float,
    retry_after: str | None = None,
) -> float:
    failure_count = _source_failures.get(source, 0) + 1
    _source_failures[source] = failure_count
    cooldown = (
        min(float(retry_after), 15 * 60)
        if retry_after and retry_after.isdigit()
        else min(base_seconds * 2 ** (failure_count - 1), 15 * 60)
    )
    _source_cooldowns[source] = time.monotonic() + cooldown
    LOGGER.warning(
        "metadata_source_paused source=%s reason=%s cooldown=%.0fs",
        source,
        reason,
        cooldown,
        extra={
            "event": "metadata_source_paused",
            "source": source,
            "reason": reason,
            "cooldown_seconds": cooldown,
        },
    )
    return cooldown


def reset_source_failures(source: str) -> None:
    _source_failures.pop(source, None)
    _source_cooldowns.pop(source, None)


# Exact aliases are intentionally used instead of substring matching.  The
# groups cover Books.com.tw and Readmoo's current top-level taxonomies plus
# unambiguous child categories that may appear without their parent in a
# product breadcrumb. Raw source values remain available for traceability.
STANDARD_CATEGORY_ALIASES = {
    "商業理財": (
        "business & economics",
        "finance",
        "財經企管",
        "商業財經",
        "商業理財",
        "經濟理論",
        "行銷企管",
        "投資理財",
        "領導管理",
        "職場工作",
    ),
    "電腦資訊": (
        "computers",
        "technology & engineering",
        "電腦資訊",
        "電腦科技",
        "程式設計",
        "作業系統",
        "網際網路",
        "數位生活",
        "電子商務",
    ),
    "文學小說": (
        "fiction",
        "poetry",
        "literary collections",
        "essays",
        "文學小說",
        "文學",
        "小說",
        "詩",
        "散文",
        "劇本",
        "輕小說",
        "童書",
        "青少年文學",
        "青少年與兒童",
        "兒童文學",
        "青少年小說",
        "羅曼史\\言情小說",
        "現代言情",
        "古典言情",
        "西洋羅曼史",
        "中國原創",
    ),
    "漫畫/圖文": (
        "comics & graphic novels",
        "漫畫",
        "圖文書",
        "漫畫/圖文書",
        "圖像小說",
        "動漫/插畫/遊戲",
    ),
    "藝術設計": (
        "art",
        "design",
        "藝術",
        "藝術設計",
        "藝術總論",
        "影視偶像",
        "電影",
        "音樂",
        "繪畫",
        "攝影",
        "建築",
        "舞蹈",
        "雕塑",
        "戲劇",
        "書法",
        "寫真集",
    ),
    "人文社科": (
        "social science",
        "law",
        "history",
        "philosophy",
        "religion",
        "社會科學",
        "人文社科",
        "人文史地",
        "人文",
        "歷史",
        "法律",
        "政治",
        "哲學",
        "宗教",
        "宗教命理",
        "人文歷史",
        "社會工作",
        "社會議題",
        "國際趨勢",
        "軍事\\戰略",
        "傳播學",
        "教育",
        "學習輔助與工具書",
        "語言學習",
        "考試用書",
        "國中小參考書",
        "參考書",
        "教科書",
        "政府出版品",
    ),
    "心理勵志": (
        "psychology",
        "self-help",
        "心理",
        "心理學",
        "心理勵志",
        "勵志成長",
        "個人成長",
        "潛能開發",
        "勵志故事",
        "人際關係",
        "兩性關係",
        "心理諮商",
        "熟齡生活",
        "生死醫病",
        "情緒壓力",
        "心靈養生",
    ),
    "自然科普": (
        "science",
        "mathematics",
        "nature",
        "自然科普",
        "物理\\化學",
        "科普叢書",
        "天文\\地球科學",
        "動\\植物",
        "環境\\科學",
        "地理",
        "數學",
        "大腦科學",
        "應用科學",
        "自然科普與應用科學",
    ),
    "醫療保健": (
        "health & fitness",
        "medical",
        "醫療保健",
        "健康養生",
        "疾病",
        "家庭醫學",
        "醫學常識",
    ),
    "飲食料理": (
        "cooking",
        "飲食",
        "飲食生活",
        "食譜",
    ),
    "生活風格": (
        "food, history",
        "dinners and dining",
        "food habits",
        "sports & recreation",
        "生活",
        "生活風格",
        "居家",
        "明星偶像",
        "運動",
        "園藝",
        "寵物",
        "手作",
        "星座命理",
        "美容美體",
        "嗜好",
        "親子教養",
        "育兒",
        "雜誌",
    ),
    "旅遊觀光": (
        "travel",
        "旅遊",
        "旅遊觀光",
        "台灣旅遊",
        "亞洲旅遊",
        "美洲旅遊",
        "歐洲旅遊",
        "非洲旅遊",
        "大洋洲旅遊",
        "環球旅遊",
    ),
}

CATEGORY_ALIASES = {
    unicodedata.normalize("NFKC", alias).casefold(): standard
    for standard, aliases in STANDARD_CATEGORY_ALIASES.items()
    for alias in aliases
}

# NCL's recommended shelf categories sometimes append a parenthetical
# explanation.  Match only these known category prefixes; keep the full value
# in raw_categories for traceability.
NCL_CATEGORY_PREFIX_ALIASES = {
    "文學小說": "文學小說",
    "人文史地": "人文社科",
    "社會科學": "人文社科",
    "財經企管": "商業理財",
    "自然科普": "自然科普",
    "醫療保健": "醫療保健",
    "藝術設計": "藝術設計",
    "生活風格": "生活風格",
    "旅遊觀光": "旅遊觀光",
}

# First three digits of common NDC-style classification numbers.
CLASSIFICATION_PREFIXES = {
    "0": "總類",
    "1": "哲學類",
    "2": "宗教類",
    "3": "自然科普",
    "4": "應用科學",
    "5": "社會科學",
    "6": "中國史地",
    "7": "世界史地",
    "8": "語言文學",
    "9": "藝術設計",
}

ROLE_ALIASES = {
    "author": "作者",
    "作者": "作者",
    "著": "作者",
    "translator": "譯者",
    "譯者": "譯者",
    "譯": "譯者",
    "illustrator": "繪者",
    "繪者": "繪者",
    "繪": "繪者",
}


@dataclass
class Contributor:
    name: str
    role: str = "作者"


@dataclass
class MetadataCandidate:
    source: str
    source_id: str | None = None
    detail_url: str | None = None
    detail_status: int | None = None
    title: str | None = None
    original_title: str | None = None
    contributors: list[Contributor] = field(default_factory=list)
    publisher: str | None = None
    edition: str | None = None
    identifiers: list[str] = field(default_factory=list)
    cover_url: str | None = None
    raw_categories: list[str] = field(default_factory=list)
    category_codes: list[str] = field(default_factory=list)
    score: float = 0.0
    matched_by: list[str] = field(default_factory=list)


def normalize_isbn(value: str | None) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def is_valid_isbn(value: str | None) -> bool:
    """Validate ISBN-10 or ISBN-13, including its checksum."""
    isbn = normalize_isbn(value)
    if len(isbn) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", isbn):
            return False
        return sum((10 - index) * (10 if char == "X" else int(char))
                   for index, char in enumerate(isbn)) % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        weighted = sum(
            int(char) * (1 if index % 2 == 0 else 3)
            for index, char in enumerate(isbn[:12])
        )
        return (10 - weighted % 10) % 10 == int(isbn[-1])
    return False


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def clean_title_for_search(title: str | None) -> str:
    """Remove only clearly promotional bracketed phrases; retain subtitles."""
    if not title:
        return ""
    marketing = r"(?:獨家|附贈|限量|典藏|套書|繁體中文版|電子書限定|特別收錄)"

    def strip_marketing(match: re.Match[str]) -> str:
        return "" if re.search(marketing, match.group(0), re.I) else match.group(0)

    cleaned = re.sub(r"[\(\（\[【][^()（）\[\]【】]{0,50}[\)）\]】]", strip_marketing, title)
    # Search providers are inconsistent about name separators and full-width
    # plus signs. Treat them as token boundaries so both sides remain
    # searchable instead of requiring an exact punctuation match.
    cleaned = re.sub(r"[‧・·+＋]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" \t-—:：")


def primary_ncl_title(title: str | None) -> str | None:
    """Keep NCL's full parallel title separately; display the part before '='."""
    if not title:
        return None
    primary = re.split(r"\s*=\s*", title, maxsplit=1)[0]
    return primary.strip() or title.strip()


def extract_clean_author(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"^(?:作者|作\s*者|繪者|譯者|著|譯|繪)\s*[：:]\s*", "", text.strip())
    # NCL commonly appends a romanized/original-language alias to a Chinese
    # display name.  Preserve it in the raw source record, but use one stable
    # display name for matching and storage.
    if re.search(r"[\u3400-\u9fff]", cleaned):
        cleaned = re.sub(
            r"\s*[\(（][A-Za-z][A-Za-z0-9 .,'’`\-]*[\)）]\s*$",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?<=[\u3400-\u9fff])\.(?=[\u3400-\u9fff])",
            "‧",
            cleaned,
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_contributor(text: str | None, role: str | None = None) -> Contributor | None:
    name = extract_clean_author(text)
    if not name:
        return None
    detected_role = role
    if not detected_role and text:
        match = re.match(r"^\s*(作者|繪者|譯者|著|譯|繪)\s*[：:]?", text)
        detected_role = match.group(1) if match else None
    return Contributor(name=name, role=ROLE_ALIASES.get((detected_role or "作者").casefold(), "作者"))


def split_classification(
    values: Iterable[str | None],
) -> tuple[list[str], list[str], str]:
    raw_categories: list[str] = []
    codes: list[str] = []
    for value in values:
        if not value:
            continue
        for part in re.split(r"\s*[>/|;；]\s*", str(value)):
            part = part.strip()
            if not part:
                continue
            if re.fullmatch(r"\d{1,3}(?:\.\d+)?", part):
                if part not in codes:
                    codes.append(part)
                continue
            if part not in raw_categories:
                raw_categories.append(part)

    # Textual source categories, especially NCL's recommended shelf category,
    # are more specific than the first digit of a classification number.
    standard = "未分類"
    for raw_category in raw_categories:
        normalized = unicodedata.normalize("NFKC", raw_category).casefold()
        mapped = CATEGORY_ALIASES.get(normalized)
        if not mapped:
            mapped = next(
                (
                    standard_name
                    for prefix, standard_name in NCL_CATEGORY_PREFIX_ALIASES.items()
                    if normalized.startswith(
                        unicodedata.normalize("NFKC", prefix).casefold()
                    )
                ),
                None,
            )
        if mapped:
            standard = mapped
            break
    if standard == "未分類":
        standard = next(
            (
                CLASSIFICATION_PREFIXES[code[0]]
                for code in codes
                if code and code[0] in CLASSIFICATION_PREFIXES
            ),
            standard,
        )
    return raw_categories, codes, standard


def clean_category(raw_category: str | None) -> str:
    """Backward-compatible standard-category helper."""
    return split_classification([raw_category])[2]


def _similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) * 0.2 + 0.8
    return SequenceMatcher(None, a, b).ratio()


def score_candidate(
    candidate: MetadataCandidate,
    *,
    isbn: str | None,
    title: str | None,
    author: str | None = None,
    publisher: str | None = None,
    edition: str | None = None,
) -> float:
    """Score an edition record. ISBN match dominates fuzzy textual evidence."""
    score = 0.0
    reasons: list[str] = []
    target_isbn = normalize_isbn(isbn)
    valid_ids = {normalize_isbn(item) for item in candidate.identifiers if is_valid_isbn(item)}
    if is_valid_isbn(target_isbn) and target_isbn in valid_ids:
        score += 0.62
        reasons.append("isbn")
    elif is_valid_isbn(target_isbn) and valid_ids:
        score -= 0.35
        reasons.append("isbn_conflict")

    isbn_targeted = is_valid_isbn(target_isbn)
    # Sources such as Readmoo may not expose ISBN in search results. In that
    # case, fall back to strict title/author evidence instead of making the
    # candidate mathematically impossible to select.
    has_identifier_evidence = isbn_targeted and bool(valid_ids)
    title_weight = 0.25 if has_identifier_evidence else 0.70
    author_weight = 0.08 if has_identifier_evidence else 0.18
    publisher_weight = 0.03 if has_identifier_evidence else 0.08
    edition_weight = 0.02 if has_identifier_evidence else 0.04

    title_score = _similarity(title, candidate.title)
    if title_score:
        score += title_score * title_weight
        if title_score >= 0.75:
            reasons.append("title")

    candidate_authors = " ".join(
        contributor.name for contributor in candidate.contributors
        if contributor.role == "作者"
    )
    author_score = _similarity(author, candidate_authors)
    if author_score:
        score += author_score * author_weight
        if author_score >= 0.75:
            reasons.append("author")

    publisher_score = _similarity(publisher, candidate.publisher)
    if publisher_score:
        score += publisher_score * publisher_weight
        if publisher_score >= 0.75:
            reasons.append("publisher")

    edition_score = _similarity(edition, candidate.edition)
    if edition_score:
        score += edition_score * edition_weight
        if edition_score >= 0.75:
            reasons.append("edition")

    if candidate.source == "books.com.tw" and candidate.detail_status == 403:
        score -= 0.10
        reasons.append("detail_http_403")

    candidate.score = round(max(0.0, min(1.0, score)), 4)
    candidate.matched_by = reasons
    return candidate.score


async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    async with _client_lock:
        if _shared_client is None or _shared_client.is_closed:
            timeout = httpx.Timeout(8.0, connect=5.0)
            _shared_client = httpx.AsyncClient(
                timeout=timeout,
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _shared_client


async def close_metadata_client() -> None:
    """Close the process-wide client during application shutdown/tests."""
    global _shared_client
    async with _client_lock:
        if _shared_client is not None and not _shared_client.is_closed:
            await _shared_client.aclose()
        _shared_client = None


async def request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    method: str = "GET",
    attempts: int = 3,
) -> httpx.Response:
    last_error: Exception | None = None
    source = urlsplit(url).netloc or url
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
            )
            if response.status_code not in RETRYABLE_STATUS:
                return response
            if attempt == attempts:
                LOGGER.warning(
                    "metadata_http_failed source=%s status=%s attempts=%s",
                    source,
                    response.status_code,
                    attempts,
                    extra={
                        "event": "metadata_http_failed",
                        "source": source,
                        "url": url,
                        "status": response.status_code,
                        "attempts": attempts,
                    },
                )
                return response
            retry_after = response.headers.get("Retry-After")
            delay = min(float(retry_after), 4.0) if retry_after and retry_after.isdigit() else 0.35 * 2 ** (attempt - 1)
            LOGGER.warning(
                "metadata_http_retry source=%s status=%s attempt=%s/%s delay=%.2fs",
                source,
                response.status_code,
                attempt,
                attempts,
                delay,
                extra={
                    "event": "metadata_http_retry",
                    "source": source,
                    "url": url,
                    "status": response.status_code,
                    "attempt": attempt,
                    "attempts": attempts,
                    "delay_seconds": round(delay, 2),
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt == attempts:
                LOGGER.error(
                    "metadata_transport_failed source=%s error=%s attempts=%s",
                    source,
                    type(exc).__name__,
                    attempts,
                    extra={
                        "event": "metadata_transport_failed",
                        "source": source,
                        "url": url,
                        "error": type(exc).__name__,
                        "attempts": attempts,
                    },
                )
                raise
            delay = 0.35 * 2 ** (attempt - 1)
            LOGGER.warning(
                "metadata_transport_retry source=%s error=%s attempt=%s/%s delay=%.2fs",
                source,
                type(exc).__name__,
                attempt,
                attempts,
                delay,
                extra={
                    "event": "metadata_transport_retry",
                    "source": source,
                    "url": url,
                    "error": type(exc).__name__,
                    "attempt": attempt,
                    "attempts": attempts,
                    "delay_seconds": round(delay, 2),
                },
            )
        await asyncio.sleep(delay + random.uniform(0, 0.1))
    assert last_error is not None
    raise last_error


def _absolute_url(url: str | None, *, default_scheme: str = "https") -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return f"{default_scheme}:{url}"
    return url


def parse_books_search(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    # Current desktop results use .table-td cards.  Keep the older selectors as
    # fallbacks so a gradual rollout or a mobile response remains parseable.
    items = soup.select(
        ".table-tr .table-td[id^='prod-itemlist-'], "
        "table.table-searchlist tbody tr, .box_1, li.item"
    )
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        title_el = item.select_one(
            "h4 a[title], h3 a[title], h3 a, .rel-name a, a.product-link"
        )
        if not title_el:
            continue
        title = title_el.get("title") or title_el.get_text(" ", strip=True)
        redirect_url = _absolute_url(title_el.get("href"))
        product_id = None
        item_id = item.get("id", "")
        if item_id.startswith("prod-itemlist-"):
            product_id = item_id.removeprefix("prod-itemlist-")
        if not product_id and redirect_url:
            match = re.search(r"/item/([^/?#]+)", redirect_url)
            product_id = match.group(1) if match else None
        detail_url = (
            f"https://www.books.com.tw/products/{product_id}"
            if product_id else redirect_url
        )
        dedupe_key = product_id or detail_url or normalize_text(title)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        author_nodes = item.select(
            "p.author a[href*='adv_author'], "
            "a[href*='search_adv_author'], .author a"
        )
        authors = [
            node.get_text(" ", strip=True)
            for node in author_nodes
            if node.get_text(" ", strip=True)
        ]
        # Books.com.tw sometimes renders one inverted western name as two
        # author links (for example "Dan" and "Jurafsky").  Rejoin only the
        # narrow two-single-token case so genuine multi-author names remain
        # separate.
        if (
            len(authors) == 2
            and all(
                re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’.-]+", name)
                for name in authors
            )
        ):
            authors = [" ".join(authors)]
        if not authors:
            author_el = item.select_one("p.author, .author")
            if author_el:
                author_text = re.sub(
                    r"^\s*作者\s*[：:]\s*", "", author_el.get_text(" ", strip=True)
                )
                authors = [
                    name for name in re.split(r"\s*[、;/；]\s*", author_text)
                    if name
                ]
        image = item.select_one("img")
        parsed.append({
            "source_id": product_id,
            "title": title,
            "authors": authors,
            # Kept for compatibility with callers/tests using the old shape.
            "author": authors[0] if authors else None,
            "cover_url": (
                image.get("data-src")
                or image.get("data-original")
                or image.get("src")
            ) if image else None,
            "detail_url": detail_url,
        })
        if len(parsed) >= MAX_RESULTS_PER_SOURCE:
            break
    return parsed


def parse_books_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    breadcrumbs = soup.select(
        "#breadcrumb-trail a, .breadcrumb li a, div.content_box ul.path li a"
    )
    categories = [
        node.get_text(" ", strip=True) for node in breadcrumbs
        if node.get_text(" ", strip=True) not in {
            "首頁", "博客來", "中文書", "商品介紹"
        }
    ]
    structured: dict[str, Any] = {}
    for script in soup.select("script[type='application/ld+json']"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        records = payload if isinstance(payload, list) else [payload]
        structured = next(
            (
                record for record in records
                if isinstance(record, dict)
                and (
                    record.get("@type") == "Book"
                    or (
                        isinstance(record.get("@type"), list)
                        and "Book" in record["@type"]
                    )
                )
            ),
            structured,
        )
        if structured:
            break

    text = soup.get_text(" ", strip=True)
    isbn_values = re.findall(
        r"ISBN(?:-1[03])?\s*[：:]?\s*([0-9Xx-]{10,17})", text
    )
    work_example = structured.get("workExample", {})
    while isinstance(work_example, dict) and isinstance(
        work_example.get("workExample"), dict
    ):
        work_example = work_example["workExample"]
    if isinstance(work_example, dict) and work_example.get("isbn"):
        isbn_values.append(str(work_example["isbn"]))
    if structured.get("isbn"):
        isbn_values.append(str(structured["isbn"]))
    identifiers = list(dict.fromkeys(
        normalized for value in isbn_values
        if is_valid_isbn(normalized := normalize_isbn(value))
    ))

    contributors: list[Contributor] = []
    role_labels = {
        "作者": "作者",
        "原文作者": "作者",
        "譯者": "譯者",
        "繪者": "繪者",
        "插畫": "繪者",
    }
    for row in soup.select(".type02_p003 > ul > li"):
        row_text = row.get_text(" ", strip=True)
        label_match = re.match(r"^(原文作者|作者|譯者|繪者|插畫)\s*[：:]", row_text)
        if not label_match:
            continue
        role = role_labels[label_match.group(1)]
        for node in row.select("a[href*='adv_author']"):
            contributor = parse_contributor(node.get_text(" ", strip=True), role)
            if contributor and contributor not in contributors:
                contributors.append(contributor)
    if not contributors:
        structured_authors = structured.get("author", [])
        if isinstance(structured_authors, (str, dict)):
            structured_authors = [structured_authors]
        for author in structured_authors if isinstance(structured_authors, list) else []:
            name = author.get("name") if isinstance(author, dict) else author
            contributor = parse_contributor(str(name or ""), "作者")
            if contributor and contributor not in contributors:
                contributors.append(contributor)

    publisher = None
    structured_publishers = structured.get("publisher", [])
    if isinstance(structured_publishers, (str, dict)):
        structured_publishers = [structured_publishers]
    for item in structured_publishers if isinstance(structured_publishers, list) else []:
        publisher = item.get("name") if isinstance(item, dict) else item
        if publisher:
            break
    if not publisher:
        publisher_row = next(
            (
                row for row in soup.select(".type02_p003 > ul > li")
                if re.match(r"^出版社\s*[：:]", row.get_text(" ", strip=True))
            ),
            None,
        )
        publisher_el = (
            publisher_row.select_one("a[href*='pubid'], a[href*='sys_puball']")
            if publisher_row else None
        )
        if not publisher_el:
            publisher_el = soup.select_one("a[href*='pubid'], a[href*='publisher']")
        publisher = publisher_el.get_text(" ", strip=True) if publisher_el else None

    return {
        "raw_categories": categories,
        "identifiers": identifiers,
        "publisher": str(publisher).strip() if publisher else None,
        "contributors": contributors,
    }


async def fetch_from_books_com(client: httpx.AsyncClient, keyword: str) -> list[MetadataCandidate]:
    source = "books.com.tw"
    if source_is_paused(source):
        log_paused_source(source)
        return []
    url = f"https://search.books.com.tw/search/query/key/{quote(keyword)}/cat/all"
    try:
        response = await request_with_retry(client, url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        pause_source(
            source,
            reason=type(exc).__name__,
            base_seconds=BOOKS_COOLDOWN_SECONDS,
        )
        raise
    if response.status_code in RETRYABLE_STATUS or response.status_code == 403:
        pause_source(
            source,
            reason=f"http_{response.status_code}",
            base_seconds=BOOKS_COOLDOWN_SECONDS,
            retry_after=response.headers.get("Retry-After"),
        )
    if response.status_code != 200:
        LOGGER.info("metadata_source_miss", extra={"source": "books.com.tw", "status": response.status_code})
        return []
    reset_source_failures(source)
    results = parse_books_search(response.text)
    candidates: list[MetadataCandidate] = []
    detail_source_available = True
    unavailable_detail_status: int | None = None
    for index, item in enumerate(results):
        detail: dict[str, Any] = {}
        detail_status = unavailable_detail_status
        if item["detail_url"] and detail_source_available:
            try:
                detail_response = await request_with_retry(client, str(item["detail_url"]))
                detail_status = detail_response.status_code
                if detail_response.status_code == 200:
                    detail = parse_books_detail(detail_response.text)
                elif (
                    detail_response.status_code in RETRYABLE_STATUS
                    or detail_response.status_code == 403
                ):
                    # Do not repeat the same retry sequence for every candidate
                    # when the detail host is already rate-limiting/unavailable.
                    detail_source_available = False
                    unavailable_detail_status = detail_response.status_code
            except (httpx.HTTPError, ValueError) as exc:
                detail_source_available = False
                detail_status = 0
                unavailable_detail_status = 0
                LOGGER.warning(
                    "metadata_detail_failed source=books.com.tw error=%s",
                    type(exc).__name__,
                    extra={
                        "event": "metadata_detail_failed",
                        "source": "books.com.tw",
                        "error": type(exc).__name__,
                    },
                )
        contributors = detail.get("contributors", [])
        if not contributors:
            contributors = [
                contributor for name in item.get("authors", [])
                if (contributor := parse_contributor(name, "作者"))
            ]
        candidates.append(MetadataCandidate(
            source="books.com.tw",
            source_id=str(item.get("source_id") or item["detail_url"] or index),
            detail_url=str(item["detail_url"]) if item["detail_url"] else None,
            detail_status=detail_status,
            title=str(item["title"]),
            contributors=contributors,
            publisher=detail.get("publisher"),
            identifiers=detail.get("identifiers", []),
            cover_url=str(item["cover_url"]) if item["cover_url"] else None,
            raw_categories=detail.get("raw_categories", []),
        ))
    return candidates


def parse_readmoo_search(html: str) -> list[MetadataCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[MetadataCandidate] = []
    items = soup.select(
        "#main_items > li.listItem-box, "
        ".rm-book-thumbnails li.listItem-box, li.listItem-box"
    )
    seen: set[str] = set()
    for index, item in enumerate(items):
        title_el = item.select_one(
            ".caption h4 a.product-link, h4 a.product-link, .book-title, h3 a"
        )
        if not title_el:
            continue
        title = title_el.get("title") or title_el.get_text(" ", strip=True)
        detail_url = _absolute_url(title_el.get("href"))
        source_id = (
            title_el.get("data-readmoo-id")
            or (item.select_one("meta[itemprop='identifier']") or {}).get("content")
        )
        if not source_id and detail_url:
            match = re.search(r"/book/([^/?#]+)", detail_url)
            source_id = match.group(1) if match else None
        dedupe_key = str(source_id or detail_url or normalize_text(title))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        contributors: list[Contributor] = []
        for node in item.select(".contributor-info a"):
            label = node.get("aria-label", "")
            role = next(
                (key for key in ("譯者", "繪者", "插畫", "作者") if key in label),
                "作者",
            )
            role = "繪者" if role == "插畫" else role
            contributor = parse_contributor(node.get_text(" ", strip=True), role)
            if contributor and contributor not in contributors:
                contributors.append(contributor)
        image = item.select_one("img.js-lazy-image, .book-cover img, img")
        category = item.select_one(".category, [data-category]")
        publisher_el = item.select_one(".publisher-info a, .publisher-info")
        candidates.append(MetadataCandidate(
            source="readmoo",
            source_id=str(source_id or item.get("data-id") or index),
            detail_url=detail_url,
            title=title,
            contributors=contributors,
            publisher=publisher_el.get_text(" ", strip=True) if publisher_el else None,
            cover_url=(
                image.get("data-lazy-original")
                or image.get("data-src")
                or image.get("src")
            ) if image else None,
            raw_categories=[category.get_text(" ", strip=True)] if category else [],
        ))
        if len(candidates) >= MAX_RESULTS_PER_SOURCE:
            break
    return candidates


def parse_readmoo_detail(html: str) -> dict[str, Any]:
    """Parse one Readmoo product selected from a search result."""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.book-detail-title, h1[itemprop='name']")
    categories = [
        node.get_text(" ", strip=True)
        for node in soup.select(".breadcrumb a")
        if node.get_text(" ", strip=True) not in {"分類導覽", "首頁"}
    ]

    contributors: list[Contributor] = []
    for row in soup.select(".book-meta-author > li"):
        row_text = row.get_text(" ", strip=True)
        label_match = re.match(r"^(作者|譯者|繪者|插畫)\s*[：:]", row_text)
        if not label_match:
            continue
        role = "繪者" if label_match.group(1) == "插畫" else label_match.group(1)
        for node in row.select(
            "a[itemprop='name'], a[href*='/contributor/']"
        ):
            contributor = parse_contributor(node.get_text(" ", strip=True), role)
            if contributor and contributor not in contributors:
                contributors.append(contributor)

    isbn_values = [
        node.get_text(" ", strip=True)
        for node in soup.select(
            ".book-meta-published [itemprop='isbn'], "
            ".book-meta-published [itemprop='eisbn']"
        )
    ]
    if not isbn_values:
        isbn_values = re.findall(
            r"(?:e?ISBN)\s*[：:]\s*([0-9Xx-]{10,17})",
            soup.get_text(" ", strip=True),
            flags=re.I,
        )
    identifiers = list(dict.fromkeys(
        normalized for value in isbn_values
        if is_valid_isbn(normalized := normalize_isbn(value))
    ))
    publisher_el = soup.select_one(
        ".book-meta-author a[itemprop='publisher'], "
        ".book-meta-author a[href*='/publisher/']"
    )
    return {
        "title": title_el.get_text(" ", strip=True) if title_el else None,
        "contributors": contributors,
        "publisher": (
            publisher_el.get_text(" ", strip=True) if publisher_el else None
        ),
        "identifiers": identifiers,
        "raw_categories": categories,
    }


async def fetch_from_readmoo_web(
    client: httpx.AsyncClient,
    keyword: str,
    raw_identifier: str | None = None,
) -> list[MetadataCandidate]:
    """Search Readmoo, then follow only the best URLs returned by that search."""
    del raw_identifier
    source = "readmoo"
    if source_is_paused(source):
        log_paused_source(source)
        return []
    url = "https://readmoo.com/search/keyword"
    try:
        response = await request_with_retry(
            client,
            url,
            params={"q": keyword, "kw": "", "page": 1},
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        pause_source(
            source,
            reason=type(exc).__name__,
            base_seconds=READMOO_COOLDOWN_SECONDS,
        )
        raise
    if response.status_code in RETRYABLE_STATUS or response.status_code == 403:
        pause_source(
            source,
            reason=f"http_{response.status_code}",
            base_seconds=READMOO_COOLDOWN_SECONDS,
            retry_after=response.headers.get("Retry-After"),
        )
    if response.status_code != 200:
        LOGGER.info("metadata_source_miss", extra={"source": "readmoo", "status": response.status_code})
        return []
    reset_source_failures(source)
    candidates = parse_readmoo_search(response.text)

    # Detail pages are followed only from actual search results.  This avoids
    # the removed ISBN-to-product URL guessing while still enriching the same
    # edition record with its ISBN, category and contributor roles.
    ranked = sorted(
        candidates,
        key=lambda candidate: SequenceMatcher(
            None,
            normalize_text(keyword),
            normalize_text(candidate.title),
        ).ratio(),
        reverse=True,
    )
    detail_source_available = True
    for candidate in ranked[:2]:
        if not candidate.detail_url or not detail_source_available:
            continue
        try:
            detail_response = await request_with_retry(client, candidate.detail_url)
            if detail_response.status_code == 200:
                detail = parse_readmoo_detail(detail_response.text)
                candidate.title = detail.get("title") or candidate.title
                candidate.contributors = (
                    detail.get("contributors") or candidate.contributors
                )
                candidate.publisher = detail.get("publisher") or candidate.publisher
                candidate.identifiers = detail.get("identifiers", [])
                candidate.raw_categories = detail.get("raw_categories", [])
            elif (
                detail_response.status_code in RETRYABLE_STATUS
                or detail_response.status_code == 403
            ):
                detail_source_available = False
        except (httpx.HTTPError, ValueError) as exc:
            detail_source_available = False
            LOGGER.warning(
                "metadata_detail_failed source=readmoo error=%s",
                type(exc).__name__,
                extra={
                    "event": "metadata_detail_failed",
                    "source": "readmoo",
                    "error": type(exc).__name__,
                },
            )
    return candidates


def parse_ncl_payload(payload: Any) -> list[MetadataCandidate]:
    rows = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    candidates: list[MetadataCandidate] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        contributors: list[Contributor] = []
        role_fields = (("author", "作者"), ("作者", "作者"), ("translator", "譯者"),
                       ("譯者", "譯者"), ("illustrator", "繪者"), ("繪者", "繪者"))
        for key, role in role_fields:
            for name in re.split(r"\s*[/,、;；]\s*", str(row.get(key, ""))):
                contributor = parse_contributor(name, role)
                if contributor and contributor not in contributors:
                    contributors.append(contributor)
        raw_classification = row.get("category") or row.get("分類號")
        raw_categories, codes, _ = split_classification([str(raw_classification) if raw_classification else None])
        row_isbn = normalize_isbn(str(row.get("isbn") or row.get("ISBN") or ""))
        original_title = row.get("title") or row.get("書名")
        candidates.append(MetadataCandidate(
            source="ncl",
            source_id=str(row.get("id") or index),
            title=primary_ncl_title(original_title),
            original_title=str(original_title) if original_title else None,
            contributors=contributors,
            publisher=row.get("publisher") or row.get("出版者"),
            edition=row.get("edition") or row.get("版本"),
            identifiers=[row_isbn] if is_valid_isbn(row_isbn) else [],
            cover_url=row.get("cover") or row.get("cover_url"),
            raw_categories=raw_categories,
            category_codes=codes,
        ))
    return candidates


def parse_ncl_contributors(value: str | None) -> list[Contributor]:
    contributors: list[Contributor] = []
    for item in re.split(r"\s*[;；]\s*", value or ""):
        item = item.strip()
        if not item:
            continue
        role = "作者"
        role_match = re.search(r"(翻譯|譯者|編著|繪者|著|作|撰|譯|繪|圖)\s*$", item)
        if role_match:
            marker = role_match.group(1)
            if marker in {"翻譯", "譯者", "譯"}:
                role = "譯者"
            elif marker in {"繪者", "繪", "圖"}:
                role = "繪者"
            item = item[:role_match.start()].strip()
        contributor = parse_contributor(item, role)
        if contributor and contributor not in contributors:
            contributors.append(contributor)
    return contributors


def parse_ncl_search_results(html: str) -> list[dict[str, str | None]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str | None]] = []
    for link in soup.select("a[href*='main_DisplayRecord.php'][href*='Pact=init']"):
        row = link.find_parent("tr")
        if row is None:
            continue
        cells = row.find_all("td", recursive=False)
        texts = [cell.get_text(" ", strip=True) for cell in cells]
        results.append({
            "title": link.get_text(" ", strip=True),
            "author": texts[3] if len(texts) > 3 else None,
            "publisher": texts[4] if len(texts) > 4 else None,
            "detail_url": link.get("href"),
        })
        if len(results) >= MAX_RESULTS_PER_SOURCE:
            break
    return results


def parse_ncl_detail(html: str, *, queried_isbn: str) -> MetadataCandidate | None:
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) != 2:
            continue
        label = cells[0].get_text(" ", strip=True).rstrip("：:")
        value = cells[1].get_text(" ", strip=True)
        if label and value:
            fields[label] = value

    original_title = fields.get("書名")
    title = primary_ncl_title(original_title)
    if not title or not original_title:
        return None
    identifiers = [
        normalize_isbn(value)
        for value in re.findall(r"(?:97[89])?[\dXx-]{10,17}", soup.get_text(" ", strip=True))
        if is_valid_isbn(value)
    ]
    if queried_isbn not in identifiers:
        identifiers.append(queried_isbn)

    raw_categories, category_codes, _ = split_classification([
        fields.get("建議上架分類"),
        fields.get("圖書類號"),
    ])
    cover = soup.select_one("img[src*='cover'], img[alt*='封面']")
    cover_url = cover.get("src") if cover else None
    if cover_url and cover_url.startswith("/"):
        cover_url = f"https://isbn.ncl.edu.tw{cover_url}"

    return MetadataCandidate(
        source="ncl",
        source_id=queried_isbn,
        title=title,
        original_title=original_title,
        contributors=parse_ncl_contributors(fields.get("作者")),
        publisher=fields.get("出版機構"),
        edition=fields.get("出版版次"),
        identifiers=list(dict.fromkeys(identifiers)),
        cover_url=cover_url,
        raw_categories=raw_categories,
        category_codes=category_codes,
    )


async def fetch_from_ncl(client: httpx.AsyncClient, isbn: str) -> list[MetadataCandidate]:
    search_page_url = (
        "https://isbn.ncl.edu.tw/NEW_ISBNNet/"
        "H30_SearchBooks.php?Pact=init4Simple&Pfuncid=281"
    )
    search_result_url = (
        "https://isbn.ncl.edu.tw/NEW_ISBNNet/"
        "main_DisplayResults.php?Pact=DisplayAll4Simple"
    )
    initial = await request_with_retry(client, search_page_url)
    if initial.status_code != 200:
        return []
    initial_soup = BeautifulSoup(initial.text, "html.parser")
    csrf = initial_soup.select_one("input[name='csrftoken']")
    data = {
        "FO_SearchField0": "ISBN",
        "FO_SearchValue0": isbn,
        "FB_clicked": "FB_開始查詢",
        "FB_pageSID": "Simple",
        "FO_Match": "2",
        "FB_Search": " 開始查詢 ",
        "FO_每頁筆數": "10",
        "FO_目前頁數": "1",
        "FB_ListOri": "",
    }
    if csrf and csrf.get("value"):
        data["csrftoken"] = str(csrf.get("value"))

    results_response = await request_with_retry(
        client,
        search_result_url,
        method="POST",
        data=data,
    )
    if results_response.status_code != 200:
        return []
    rows = parse_ncl_search_results(results_response.text)
    candidates: list[MetadataCandidate] = []
    for row in rows:
        detail_url = row.get("detail_url")
        if not detail_url:
            continue
        detail_response = await request_with_retry(
            client,
            str(httpx.URL(search_result_url).join(str(detail_url))),
        )
        if detail_response.status_code != 200:
            continue
        candidate = parse_ncl_detail(detail_response.text, queried_isbn=isbn)
        if candidate:
            candidates.append(candidate)
    return candidates


def parse_google_books_payload(payload: Any) -> list[MetadataCandidate]:
    candidates: list[MetadataCandidate] = []
    for item in payload.get("items", [])[:MAX_RESULTS_PER_SOURCE] if isinstance(payload, dict) else []:
        info = item.get("volumeInfo", {})
        title = info.get("title")
        subtitle = info.get("subtitle")
        display_title = (
            f"{title}: {subtitle}"
            if title and subtitle and normalize_text(subtitle) not in normalize_text(title)
            else title
        )
        identifiers = [
            normalize_isbn(entry.get("identifier"))
            for entry in info.get("industryIdentifiers", [])
            if is_valid_isbn(entry.get("identifier"))
        ]
        contributors = [
            Contributor(name=name, role="作者")
            for name in info.get("authors", []) if name
        ]
        image_links = info.get("imageLinks", {})
        candidates.append(MetadataCandidate(
            source="google_books",
            source_id=item.get("id"),
            title=display_title,
            original_title=title,
            contributors=contributors,
            publisher=info.get("publisher"),
            edition=info.get("contentVersion"),
            identifiers=identifiers,
            cover_url=image_links.get("thumbnail") or image_links.get("smallThumbnail"),
            raw_categories=[str(value) for value in info.get("categories", [])],
        ))
    return candidates


async def fetch_from_google_books(
    client: httpx.AsyncClient,
    query: str,
) -> list[MetadataCandidate]:
    source = "google_books"
    async with _google_books_lock:
        cooldown_until = _source_cooldowns.get(source, 0.0)
        if cooldown_until > time.monotonic():
            log_paused_source(source)
            return []

        params = {"q": query, "maxResults": str(MAX_RESULTS_PER_SOURCE)}
        if GOOGLE_BOOKS_API_KEY:
            params["key"] = GOOGLE_BOOKS_API_KEY
        response = await request_with_retry(
            client,
            "https://www.googleapis.com/books/v1/volumes",
            params=params,
            attempts=2,
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            pause_source(
                source,
                reason="http_429",
                base_seconds=GOOGLE_BOOKS_COOLDOWN_SECONDS,
                retry_after=retry_after,
            )
            return []
        if response.status_code != 200:
            return []
        reset_source_failures(source)
        try:
            return parse_google_books_payload(response.json())
        except ValueError as exc:
            LOGGER.warning(
                "metadata_parse_failed source=google_books error=%s",
                type(exc).__name__,
                extra={
                    "event": "metadata_parse_failed",
                    "source": source,
                    "error": str(exc),
                },
            )
            return []


def parse_open_library_payload(payload: Any) -> list[MetadataCandidate]:
    """Parse edition records returned by Open Library's ISBN Read API."""
    if not isinstance(payload, dict):
        return []
    records = payload.get("records", {})
    if not isinstance(records, dict):
        return []

    candidates: list[MetadataCandidate] = []
    for record_key, record in list(records.items())[:MAX_RESULTS_PER_SOURCE]:
        if not isinstance(record, dict):
            continue
        data = record.get("data", {})
        if not isinstance(data, dict):
            continue
        title = data.get("title")
        subtitle = data.get("subtitle")
        display_title = (
            f"{title}: {subtitle}"
            if title and subtitle and normalize_text(subtitle) not in normalize_text(title)
            else title
        )
        identifiers = [
            normalized
            for value in record.get("isbns", [])
            if is_valid_isbn(normalized := normalize_isbn(str(value)))
        ]
        data_identifiers = data.get("identifiers", {})
        if isinstance(data_identifiers, dict):
            for key in ("isbn_13", "isbn_10"):
                for value in data_identifiers.get(key, []):
                    normalized = normalize_isbn(str(value))
                    if is_valid_isbn(normalized):
                        identifiers.append(normalized)

        contributors = []
        for author in data.get("authors", []):
            name = author.get("name") if isinstance(author, dict) else author
            contributor = parse_contributor(str(name or ""), "作者")
            if contributor and contributor not in contributors:
                contributors.append(contributor)

        publisher = None
        for item in data.get("publishers", []):
            publisher = item.get("name") if isinstance(item, dict) else item
            if publisher:
                break
        subjects = [
            str(subject.get("name") if isinstance(subject, dict) else subject)
            for subject in data.get("subjects", [])[:10]
            if subject
        ]
        cover_url = None
        for item in payload.get("items", []):
            if not isinstance(item, dict) or item.get("fromRecord") != record_key:
                continue
            cover = item.get("cover", {})
            if isinstance(cover, dict):
                cover_url = cover.get("large") or cover.get("medium")
            if cover_url:
                break
        identifiers = list(dict.fromkeys(identifiers))
        if not cover_url and identifiers:
            cover_url = (
                f"https://covers.openlibrary.org/b/isbn/{identifiers[0]}-L.jpg"
            )
        detail_url = data.get("url") or record.get("recordURL")
        if isinstance(detail_url, str) and detail_url.startswith("http://"):
            detail_url = "https://" + detail_url.removeprefix("http://")
        candidates.append(MetadataCandidate(
            source="open_library",
            source_id=str(data.get("key") or record_key),
            detail_url=str(detail_url) if detail_url else None,
            title=str(display_title) if display_title else None,
            original_title=str(title) if title else None,
            contributors=contributors,
            publisher=str(publisher).strip() if publisher else None,
            identifiers=identifiers,
            cover_url=cover_url,
            raw_categories=subjects,
        ))
    return candidates


async def fetch_from_open_library(
    client: httpx.AsyncClient,
    isbn: str,
) -> list[MetadataCandidate]:
    """Fetch an exact Open Library edition by ISBN without an API key."""
    source = "open_library"
    if source_is_paused(source):
        log_paused_source(source)
        return []
    normalized_isbn = normalize_isbn(isbn)
    if not is_valid_isbn(normalized_isbn):
        return []
    url = (
        "https://openlibrary.org/api/volumes/brief/"
        f"isbn/{normalized_isbn}.json"
    )
    try:
        response = await request_with_retry(client, url, attempts=2)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        pause_source(
            source,
            reason=type(exc).__name__,
            base_seconds=OPEN_LIBRARY_COOLDOWN_SECONDS,
        )
        raise
    if response.status_code in RETRYABLE_STATUS or response.status_code == 403:
        pause_source(
            source,
            reason=f"http_{response.status_code}",
            base_seconds=OPEN_LIBRARY_COOLDOWN_SECONDS,
            retry_after=response.headers.get("Retry-After"),
        )
    if response.status_code != 200:
        return []
    reset_source_failures(source)
    try:
        return parse_open_library_payload(response.json())
    except ValueError as exc:
        LOGGER.warning(
            "metadata_parse_failed source=open_library error=%s",
            type(exc).__name__,
            extra={
                "event": "metadata_parse_failed",
                "source": source,
                "error": str(exc),
            },
        )
        return []


def build_search_queries(
    isbn: str | None,
    title: str | None,
    author: str | None = None,
    publisher: str | None = None,
    edition: str | None = None,
) -> list[str]:
    queries: list[str] = []
    if is_valid_isbn(isbn):
        queries.append(normalize_isbn(isbn))
    clean_title = clean_title_for_search(title)
    for parts in (
        [clean_title],
        [clean_title, author],
        [clean_title, publisher],
        [clean_title, edition],
    ):
        query = " ".join(str(part).strip() for part in parts if part and str(part).strip())
        if query and query not in queries:
            queries.append(query)
    return queries


async def _lookup_metadata(
    isbn: str,
    raw_title: str | None,
    author: str | None,
    publisher: str | None,
    edition: str | None,
) -> dict[str, Any]:
    identifier = normalize_isbn(isbn)
    valid_isbn = is_valid_isbn(identifier)
    LOGGER.info(
        "metadata_lookup_started identifier=%s isbn_valid=%s title=%s",
        identifier,
        valid_isbn,
        (raw_title or "")[:60],
        extra={
            "event": "metadata_lookup_started",
            "identifier": identifier,
            "isbn_valid": valid_isbn,
            "title": (raw_title or "")[:60],
        },
    )
    if len(identifier) in {10, 13} and not valid_isbn:
        LOGGER.warning(
            "metadata_invalid_isbn identifier=%s",
            identifier,
            extra={"event": "metadata_invalid_isbn", "identifier": identifier},
        )

    client = await get_shared_client()
    queries = build_search_queries(identifier if valid_isbn else None, raw_title, author, publisher, edition)
    primary_query = queries[0] if queries else identifier
    title_query = next((query for query in queries if query != identifier), primary_query)
    candidates: list[MetadataCandidate] = []
    minimum_score = 0.58 if valid_isbn else 0.48

    def has_reliable_match(
        source_name: str,
        source_candidates: list[MetadataCandidate],
    ) -> bool:
        for candidate in source_candidates:
            score_candidate(
                candidate,
                isbn=identifier if valid_isbn else None,
                title=raw_title,
                author=author,
                publisher=publisher,
                edition=edition,
            )
        best_candidate = max(
            source_candidates,
            key=lambda candidate: candidate.score,
            default=None,
        )
        max_score = best_candidate.score if best_candidate else 0.0
        reliable = best_candidate is not None and max_score >= minimum_score
        reliability_reason = "score"
        if (
            reliable
            and source_name == "books.com.tw"
            and best_candidate.detail_status == 403
        ):
            reliable = False
            reliability_reason = "detail_http_403"
        # A Books search-card title is useful, but when its blocked detail page
        # exposes no ISBN it has not proven that it is the requested edition.
        if reliable and valid_isbn and source_name == "books.com.tw":
            reliable = identifier in {
                normalize_isbn(value)
                for value in best_candidate.identifiers
                if is_valid_isbn(value)
            }
            if not reliable:
                reliability_reason = "isbn_unverified"
        LOGGER.info(
            "metadata_source_result source=%s candidates=%s max_score=%.4f threshold=%.2f reliable=%s reason=%s",
            source_name,
            len(source_candidates),
            max_score,
            minimum_score,
            reliable,
            reliability_reason,
            extra={
                "event": "metadata_source_result",
                "source": source_name,
                "candidate_count": len(source_candidates),
                "max_score": max_score,
                "threshold": minimum_score,
                "reliable": reliable,
                "reason": reliability_reason,
            },
        )
        return reliable

    async def run_source(
        source_name: str,
        operation: Any,
    ) -> list[MetadataCandidate]:
        LOGGER.info(
            "metadata_source_started source=%s",
            source_name,
            extra={"event": "metadata_source_started", "source": source_name},
        )
        try:
            return await operation
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.error(
                "metadata_source_failed source=%s error=%s",
                source_name,
                type(exc).__name__,
                extra={
                    "event": "metadata_source_failed",
                    "source": source_name,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return []

    # Real source priority: a reliable result stops the fallback chain. This
    # avoids spending Google quota when NCL or Books.com.tw already matched.
    matched = False
    if valid_isbn:
        ncl_candidates = await run_source("ncl", fetch_from_ncl(client, identifier))
        candidates.extend(ncl_candidates)
        matched = has_reliable_match("ncl", ncl_candidates)
    else:
        LOGGER.info(
            "metadata_source_skipped source=ncl reason=invalid_or_missing_isbn",
            extra={
                "event": "metadata_source_skipped",
                "source": "ncl",
                "reason": "invalid_or_missing_isbn",
            },
        )

    if not matched:
        books_candidates = await run_source(
            "books.com.tw",
            fetch_from_books_com(client, primary_query),
        )
        candidates.extend(books_candidates)
        matched = has_reliable_match("books.com.tw", books_candidates)

        # Try at most two title/author/publisher variants before leaving the
        # preferred Books.com.tw source.
        if not matched and not books_candidates and len(queries) > 1:
            for query in queries[1:3]:
                if query == primary_query:
                    continue
                extra_books = await run_source(
                    "books.com.tw",
                    fetch_from_books_com(client, query),
                )
                candidates.extend(extra_books)
                if has_reliable_match("books.com.tw", extra_books):
                    matched = True
                    break

    if not matched:
        readmoo_candidates = await run_source(
            "readmoo",
            fetch_from_readmoo_web(client, title_query),
        )
        candidates.extend(readmoo_candidates)
        matched = has_reliable_match("readmoo", readmoo_candidates)

    # Open Library requires no API key, so try its exact-edition ISBN endpoint
    # before spending Google Books quota.
    if not matched and valid_isbn:
        open_library_candidates = await run_source(
            "open_library",
            fetch_from_open_library(client, identifier),
        )
        candidates.extend(open_library_candidates)
        matched = has_reliable_match("open_library", open_library_candidates)

    # Google Books remains last because the API has a finite quota.
    if not matched:
        google_candidates = await run_source(
            "google_books",
            fetch_from_google_books(
                client,
                f"isbn:{identifier}" if valid_isbn else f"intitle:{title_query}",
            ),
        )
        candidates.extend(google_candidates)
        matched = has_reliable_match("google_books", google_candidates)

    unique: dict[tuple[str, str], MetadataCandidate] = {}
    for candidate in candidates:
        key = (candidate.source, candidate.source_id or normalize_text(candidate.title))
        unique.setdefault(key, candidate)
    candidates = list(unique.values())

    for candidate in candidates:
        score_candidate(
            candidate,
            isbn=identifier if valid_isbn else None,
            title=raw_title,
            author=author,
            publisher=publisher,
            edition=edition,
        )
    candidates.sort(key=lambda item: item.score, reverse=True)

    # A Books.com.tw search card can help decide whether to continue to later
    # sources, but a blocked detail page has not supplied an edition record.
    # Never let that incomplete card outrank a fully parsed fallback candidate.
    selectable_candidates = [
        candidate
        for candidate in candidates
        if not (
            candidate.source == "books.com.tw"
            and candidate.detail_status == 403
        )
    ]
    best = selectable_candidates[0] if selectable_candidates else None

    # A weak textual candidate is less trustworthy than the caller's own title.
    selected = best if best and best.score >= minimum_score else None
    raw_categories, category_codes, standard_category = split_classification(
        [*(selected.raw_categories if selected else []), *(selected.category_codes if selected else [])]
    )
    selected_author = next(
        (person.name for person in selected.contributors if person.role == "作者"),
        None,
    ) if selected else None
    cover_url = selected.cover_url if selected else None
    if cover_url and cover_url.startswith("http://"):
        cover_url = "https://" + cover_url.removeprefix("http://")

    source_summary = [
        {
            "source": candidate.source,
            "source_id": candidate.source_id,
            "confidence": candidate.score,
            "matched_by": candidate.matched_by,
        }
        for candidate in candidates[:10]
    ]
    result: dict[str, Any] = {
        "isbn": identifier,
        "isbn_valid": valid_isbn,
        "title": selected.title if selected and selected.title else raw_title or f"未命名書籍 ({identifier})",
        "original_title": (
            selected.original_title or selected.title
            if selected else raw_title
        ),
        "author": selected_author or author or "未知作者",
        "contributors": [asdict(person) for person in selected.contributors] if selected else [],
        "publisher": selected.publisher if selected else publisher,
        "edition": selected.edition if selected else edition,
        "identifiers": list(selected.identifiers) if selected else [],
        "cover_url": cover_url,
        "raw_categories": raw_categories,
        "category_codes": category_codes,
        "standard_category": standard_category,
        "category": standard_category,  # backward compatibility for Book.category
        "source": selected.source if selected else None,
        "confidence": selected.score if selected else 0.0,
        "sources": source_summary,
    }
    LOGGER.info(
        "metadata_lookup_completed identifier=%s source=%s confidence=%.4f candidates=%s",
        identifier,
        result["source"] or "none",
        result["confidence"],
        len(candidates),
        extra={
            "event": "metadata_lookup_completed",
            "isbn": identifier,
            "candidate_count": len(candidates),
            "selected_source": result["source"],
            "confidence": result["confidence"],
        },
    )
    return result


async def fetch_and_clean_metadata(
    isbn: str,
    raw_title: str | None = None,
    *,
    author: str | None = None,
    publisher: str | None = None,
    edition: str | None = None,
) -> dict[str, Any]:
    """Fetch the best matching edition, with TTL caching and in-flight dedupe."""
    key = (
        normalize_isbn(isbn),
        normalize_text(raw_title),
        normalize_text(author),
        normalize_text(publisher),
        normalize_text(edition),
    )
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        LOGGER.info(
            "metadata_cache_hit identifier=%s",
            key[0],
            extra={"event": "metadata_cache_hit", "identifier": key[0]},
        )
        return dict(cached[1])

    async with _inflight_lock:
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                _lookup_metadata(isbn, raw_title, author, publisher, edition)
            )
            _inflight[key] = task
    try:
        result = await asyncio.shield(task)
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
        return dict(result)
    finally:
        if task.done():
            async with _inflight_lock:
                if _inflight.get(key) is task:
                    _inflight.pop(key, None)
