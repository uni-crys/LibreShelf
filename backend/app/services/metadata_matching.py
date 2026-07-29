"""Evidence-based decisions for applying third-party book metadata."""

from dataclasses import dataclass
from enum import Enum
from difflib import SequenceMatcher
import re
from typing import Any
import unicodedata

from app.models import Book
from app.services.metadata_pipeline import (
    is_valid_isbn,
    normalize_isbn,
    normalize_text,
)


class MetadataMatchAction(str, Enum):
    CANONICALIZE = "canonicalize"
    ENRICH_ONLY = "enrich_only"
    PRESERVE_RAW = "preserve_raw"
    REJECT = "reject"


@dataclass(frozen=True)
class MetadataMatchEvidence:
    isbn_match: bool
    isbn_conflict: bool
    volume_conflict: bool
    title_similarity: float
    author_similarity: float
    publisher_similarity: float
    edition_similarity: float
    cover_similarity: float | None
    corroborating_fields: int
    source_confidence: float


@dataclass(frozen=True)
class MetadataMatchDecision:
    action: MetadataMatchAction
    confidence: float
    canonical_isbn: str | None
    reasons: tuple[str, ...]
    evidence: MetadataMatchEvidence

    @property
    def may_enrich(self) -> bool:
        return self.action in {
            MetadataMatchAction.CANONICALIZE,
            MetadataMatchAction.ENRICH_ONLY,
        }


def _isbn13(value: str | None) -> str:
    isbn = normalize_isbn(value)
    if len(isbn) == 13 and is_valid_isbn(isbn):
        return isbn
    if len(isbn) != 10 or not is_valid_isbn(isbn):
        return ""
    body = f"978{isbn[:9]}"
    weighted = sum(
        int(character) * (1 if index % 2 == 0 else 3)
        for index, character in enumerate(body)
    )
    return f"{body}{(10 - weighted % 10) % 10}"


def _similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        return 0.8 + ratio * 0.2
    return SequenceMatcher(None, a, b).ratio()


_EDITION_MARKER = re.compile(
    r"[\(\[【]?\s*(?:第)?[0-9一二三四五六七八九十]+\s*版\s*[\)\]】]?",
    re.I,
)
_EXPLICIT_VOLUME_MARKER = re.compile(
    r"(?:第\s*)?([1-9]\d?|[一二三四五六七八九十]+)\s*(?:冊|集|卷|部)",
    re.I,
)
_TRAILING_VOLUME_MARKER = re.compile(
    r"(?<=[^\W\d_])([1-9]\d?|[ivx]{1,4})\s*(?=[:：]|$)",
    re.I,
)


def _volume_marker(title: str | None) -> str | None:
    """Extract a sequel/volume marker without treating 二版 as volume two."""
    value = unicodedata.normalize("NFKC", title or "").casefold()
    value = _EDITION_MARKER.sub("", value)
    explicit = _EXPLICIT_VOLUME_MARKER.search(value)
    if explicit:
        return explicit.group(1)
    trailing = _TRAILING_VOLUME_MARKER.search(value)
    return trailing.group(1) if trailing else None


def _has_volume_conflict(
    raw_title: str,
    metadata_title: str | None,
    *,
    isbn_match: bool,
) -> bool:
    if isbn_match:
        return False
    raw_volume = _volume_marker(raw_title)
    candidate_volume = _volume_marker(metadata_title)
    if raw_volume == candidate_volume:
        return False
    # Only classify this as a semantic conflict when the titles otherwise
    # resemble one another. Unrelated titles are rejected by the normal score.
    return _similarity(raw_title, metadata_title) >= 0.75


def _metadata_authors(metadata: dict[str, Any]) -> str:
    contributors = metadata.get("contributors") or []
    authors = [
        str(person.get("name") or "").strip()
        for person in contributors
        if isinstance(person, dict) and person.get("role") == "作者"
    ]
    return " ".join(filter(None, authors)) or str(
        metadata.get("author") or ""
    )


def decide_metadata_match(
    *,
    identifier: str | None,
    raw_title: str,
    metadata: dict[str, Any],
    raw_author: str | None = None,
    raw_publisher: str | None = None,
    raw_edition: str | None = None,
    cover_similarity: float | None = None,
) -> MetadataMatchDecision:
    """Classify whether metadata may identify or merely enrich a platform book."""
    reasons: list[str] = []
    target_isbn = _isbn13(identifier)
    candidate_isbns = {
        converted
        for value in [
            *(metadata.get("identifiers") or []),
            metadata.get("isbn") if metadata.get("isbn_valid") else None,
        ]
        if (converted := _isbn13(str(value or "")))
    }

    isbn_match = bool(target_isbn and target_isbn in candidate_isbns)
    isbn_conflict = bool(
        target_isbn and candidate_isbns and target_isbn not in candidate_isbns
    )
    if isbn_match:
        reasons.append("isbn")
    if isbn_conflict:
        reasons.append("isbn_conflict")

    title_similarity = _similarity(raw_title, metadata.get("title"))
    volume_conflict = _has_volume_conflict(
        raw_title,
        metadata.get("title"),
        isbn_match=isbn_match,
    )
    if volume_conflict:
        reasons.append("volume_conflict")
    author_similarity = _similarity(
        raw_author,
        _metadata_authors(metadata),
    )
    publisher_similarity = _similarity(
        raw_publisher,
        metadata.get("publisher"),
    )
    edition_similarity = _similarity(
        raw_edition,
        metadata.get("edition"),
    )

    corroborating_fields = sum(
        (
            title_similarity >= 0.85,
            bool(raw_author) and author_similarity >= 0.8,
            bool(raw_publisher) and publisher_similarity >= 0.8,
            bool(raw_edition) and edition_similarity >= 0.8,
            cover_similarity is not None and cover_similarity >= 0.8,
        )
    )
    if title_similarity >= 0.85:
        reasons.append("title")
    if raw_author and author_similarity >= 0.8:
        reasons.append("author")
    if raw_publisher and publisher_similarity >= 0.8:
        reasons.append("publisher")
    if raw_edition and edition_similarity >= 0.8:
        reasons.append("edition")
    if cover_similarity is not None and cover_similarity >= 0.8:
        reasons.append("cover")

    source_confidence = max(
        0.0,
        min(1.0, float(metadata.get("confidence") or 0.0)),
    )
    evidence_score = (
        (0.55 if isbn_match else 0.0)
        + title_similarity * 0.25
        + author_similarity * 0.10
        + publisher_similarity * 0.05
        + edition_similarity * 0.03
        + (cover_similarity or 0.0) * 0.02
    )
    confidence = round(
        max(source_confidence, min(1.0, evidence_score)),
        4,
    )
    evidence = MetadataMatchEvidence(
        isbn_match=isbn_match,
        isbn_conflict=isbn_conflict,
        volume_conflict=volume_conflict,
        title_similarity=round(title_similarity, 4),
        author_similarity=round(author_similarity, 4),
        publisher_similarity=round(publisher_similarity, 4),
        edition_similarity=round(edition_similarity, 4),
        cover_similarity=(
            round(cover_similarity, 4)
            if cover_similarity is not None
            else None
        ),
        corroborating_fields=corroborating_fields,
        source_confidence=round(source_confidence, 4),
    )

    if isbn_conflict:
        return MetadataMatchDecision(
            MetadataMatchAction.REJECT,
            confidence,
            target_isbn,
            tuple(reasons),
            evidence,
        )

    if volume_conflict:
        return MetadataMatchDecision(
            MetadataMatchAction.REJECT,
            confidence,
            target_isbn or None,
            tuple(reasons),
            evidence,
        )

    if isbn_match and confidence >= 0.78:
        return MetadataMatchDecision(
            MetadataMatchAction.CANONICALIZE,
            confidence,
            target_isbn,
            tuple(reasons),
            evidence,
        )

    normalized_raw_title = normalize_text(raw_title)
    normalized_candidate_title = normalize_text(metadata.get("title"))
    short_title_conflict = (
        0 < len(normalized_raw_title) <= 3
        and normalized_candidate_title != normalized_raw_title
    )
    if short_title_conflict:
        return MetadataMatchDecision(
            MetadataMatchAction.REJECT,
            confidence,
            target_isbn or None,
            (*reasons, "short_title_conflict"),
            evidence,
        )

    canonical_candidates = sorted(candidate_isbns)
    if (
        not target_isbn
        and len(canonical_candidates) == 1
        and confidence >= 0.85
        and title_similarity >= 0.85
        and corroborating_fields >= 2
    ):
        return MetadataMatchDecision(
            MetadataMatchAction.CANONICALIZE,
            confidence,
            canonical_candidates[0],
            tuple(reasons),
            evidence,
        )

    if confidence >= 0.70 and title_similarity >= 0.85:
        return MetadataMatchDecision(
            MetadataMatchAction.ENRICH_ONLY,
            confidence,
            target_isbn or None,
            tuple(reasons),
            evidence,
        )

    action = (
        MetadataMatchAction.REJECT
        if metadata.get("source") and title_similarity < 0.60
        else MetadataMatchAction.PRESERVE_RAW
    )
    return MetadataMatchDecision(
        action,
        confidence,
        target_isbn or None,
        tuple(reasons),
        evidence,
    )


def metadata_book_values(
    decision: MetadataMatchDecision,
    *,
    raw_title: str,
    crawler_cover: str | None,
    metadata: dict[str, Any],
) -> dict[str, str | None]:
    trusted = decision.may_enrich
    return {
        "title": (
            metadata.get("title")
            if decision.action == MetadataMatchAction.CANONICALIZE
            else raw_title
        ) or raw_title,
        "author": (
            metadata.get("author") if trusted else None
        ) or "未知作者",
        "cover_url": (
            metadata.get("cover_url") if trusted else crawler_cover
        ) or crawler_cover,
        "category": (
            metadata.get("standard_category")
            or metadata.get("category")
            if trusted
            else None
        ) or "未分類",
    }


def apply_metadata_decision(
    book: Book,
    decision: MetadataMatchDecision,
    *,
    raw_title: str,
    crawler_cover: str | None,
    metadata: dict[str, Any],
) -> bool:
    """Fill incomplete fields without overwriting already useful values."""
    before = (book.title, book.author, book.cover_url, book.category)
    values = metadata_book_values(
        decision,
        raw_title=raw_title,
        crawler_cover=crawler_cover,
        metadata=metadata,
    )
    if not str(book.title or "").strip() or book.title == "未知書名":
        book.title = str(values["title"] or raw_title)
    if not str(book.author or "").strip() or book.author == "未知作者":
        book.author = str(values["author"] or "未知作者")
    if not str(book.cover_url or "").strip() and values["cover_url"]:
        book.cover_url = str(values["cover_url"])
    if (
        not str(book.category or "").strip()
        or book.category in {"未分類", "Unknown", "Unkown"}
    ):
        book.category = str(values["category"] or "未分類")
    return before != (book.title, book.author, book.cover_url, book.category)


def apply_platform_snapshot(
    book: Book,
    *,
    platform_book_id: str,
    raw_title: str,
    crawler_cover: str | None,
) -> bool:
    """Repair stale metadata while a platform ID is still the local book key.

    Once a platform ID has been mapped to a canonical ISBN, the canonical
    edition remains authoritative and this function deliberately does nothing.
    """
    unresolved_platform_book = (
        str(book.isbn or "").strip() == str(platform_book_id or "").strip()
        and not is_valid_isbn(book.isbn)
    )
    if not unresolved_platform_book:
        return False

    before = (book.title, book.cover_url)
    title_changed = bool(raw_title.strip()) and (
        normalize_text(book.title) != normalize_text(raw_title)
    )
    if title_changed:
        book.title = raw_title.strip()
    usable_crawler_cover = bool(
        crawler_cover
        and "openbook.png" not in crawler_cover.casefold()
        and "placeholder" not in crawler_cover.casefold()
    )
    if usable_crawler_cover and (title_changed or not book.cover_url):
        book.cover_url = crawler_cover
    return before != (book.title, book.cover_url)
