"""Run metadata lookups against known books without touching the database.

Usage:
    PYTHONPATH=. python3 scripts/test_metadata_samples.py
    PYTHONPATH=. python3 scripts/test_metadata_samples.py --limit 2 --delay 3
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.services.metadata_pipeline import (
    close_metadata_client,
    fetch_from_books_com,
    fetch_from_google_books,
    fetch_from_ncl,
    fetch_from_open_library,
    fetch_from_readmoo_web,
    fetch_and_clean_metadata,
    get_shared_client,
    normalize_text,
    score_candidate,
    split_classification,
)


@dataclass(frozen=True)
class ExpectedBook:
    title: str
    category: str
    author: str
    isbn: str


SAMPLES = [
    ExpectedBook("少年粉紅", "文學小說", "潘柏霖", "9789571078304"),
    ExpectedBook(
        "哈利波特(1)：神秘的魔法石【繁體中文版20週年紀念】",
        "文學小說",
        "J.K.羅琳",
        "9789573335566",
    ),
    ExpectedBook("在小山和小山之間", "文學小說", "李停", "9786267721834"),
    ExpectedBook(
        "剩餘的滋味：人類的食物保存戰與浪費史",
        "人文社科",
        "埃莉諾‧巴尼特",
        "9786264560160",
    ),
    ExpectedBook(
        "不正義的地理學：二戰後東亞的記憶戰爭與歷史裂痕",
        "人文社科",
        "顧若鵬",
        "9789862627785",
    ),
    ExpectedBook(
        "The Language of Food: A Linguist Reads the Menu",
        "生活風格",
        "Jurafsky, Dan",
        "9780393351620",
    ),
]


def text_matches(expected: str, actual: str | None, threshold: float = 0.78) -> bool:
    left = normalize_text(expected)
    right = normalize_text(actual)
    if not left or not right:
        return False
    return SequenceMatcher(None, left, right).ratio() >= threshold


def author_matches(expected: str, actual: str | None) -> bool:
    if text_matches(expected, actual):
        return True
    left_tokens = sorted(
        token.casefold()
        for token in re.findall(r"[A-Za-z]+", expected)
    )
    right_tokens = sorted(
        token.casefold()
        for token in re.findall(r"[A-Za-z]+", actual or "")
    )
    return bool(left_tokens) and left_tokens == right_tokens


def print_field(label: str, expected: str, actual: str | None, passed: bool) -> None:
    marker = "PASS" if passed else "FAIL"
    print(f"  [{marker}] {label}: expected={expected!r} actual={actual!r}")


async def fetch_source_only(expected: ExpectedBook, source: str) -> dict:
    client = await get_shared_client()
    if source == "ncl":
        candidates = await fetch_from_ncl(client, expected.isbn)
    elif source == "books":
        candidates = await fetch_from_books_com(client, expected.isbn)
    elif source == "readmoo":
        candidates = await fetch_from_readmoo_web(client, expected.title)
    elif source == "google":
        candidates = await fetch_from_google_books(
            client,
            f"isbn:{expected.isbn}",
        )
    elif source == "openlibrary":
        candidates = await fetch_from_open_library(client, expected.isbn)
    else:
        raise ValueError(f"unsupported source: {source}")
    for candidate in candidates:
        score_candidate(
            candidate,
            isbn=expected.isbn,
            title=expected.title,
        )
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    selected = candidates[0] if candidates and candidates[0].score >= 0.58 else None
    if not selected:
        return {
            "isbn": expected.isbn,
            "isbn_valid": True,
            "title": expected.title,
            "author": "未知作者",
            "category": "未分類",
            "source": None,
            "confidence": 0.0,
            "sources": [],
        }
    _, _, category = split_classification([
        *selected.raw_categories,
        *selected.category_codes,
    ])
    author = next(
        (
            contributor.name
            for contributor in selected.contributors
            if contributor.role == "作者"
        ),
        "未知作者",
    )
    return {
        "isbn": expected.isbn,
        "isbn_valid": True,
        "title": selected.title,
        "author": author,
        "category": category,
        "source": selected.source,
        "confidence": selected.score,
        "sources": candidates,
    }


async def run(start: int, limit: int, delay: float, source: str) -> int:
    failed_books = 0
    selected_samples = SAMPLES[start - 1:start - 1 + limit]
    try:
        for offset, expected in enumerate(selected_samples):
            index = start + offset
            print(f"\n--- {index}. {expected.title} ({expected.isbn}) ---")
            if source != "pipeline":
                actual = await fetch_source_only(expected, source)
            else:
                actual = await fetch_and_clean_metadata(
                    isbn=expected.isbn,
                    raw_title=expected.title,
                )

            retrieved = actual.get("source") is not None
            title_ok = retrieved and text_matches(expected.title, actual.get("title"))
            author_ok = retrieved and author_matches(expected.author, actual.get("author"))
            category_ok = retrieved and expected.category == actual.get("category")
            isbn_ok = expected.isbn == actual.get("isbn") and actual.get("isbn_valid") is True

            print_field("isbn", expected.isbn, actual.get("isbn"), isbn_ok)
            print_field("title", expected.title, actual.get("title"), title_ok)
            print_field("author", expected.author, actual.get("author"), author_ok)
            print_field("category", expected.category, actual.get("category"), category_ok)
            print(
                "  retrieved={} source={!r} confidence={} candidates={}".format(
                    retrieved,
                    actual.get("source"),
                    actual.get("confidence"),
                    len(actual.get("sources", [])),
                )
            )

            if not all((isbn_ok, title_ok, author_ok, category_ok)):
                failed_books += 1
            if offset < len(selected_samples) - 1 and delay > 0:
                await asyncio.sleep(delay)
    finally:
        await close_metadata_client()

    tested = len(selected_samples)
    print(f"\nSummary: tested={tested} passed={tested - failed_books} failed={failed_books}")
    return 1 if failed_books else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, choices=range(1, len(SAMPLES) + 1))
    parser.add_argument("--limit", type=int, default=len(SAMPLES), choices=range(1, len(SAMPLES) + 1))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument(
        "--source",
        choices=("pipeline", "ncl", "books", "readmoo", "openlibrary", "google"),
        default="pipeline",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(args.start, args.limit, max(args.delay, 0.0), args.source)
    )


if __name__ == "__main__":
    raise SystemExit(main())
