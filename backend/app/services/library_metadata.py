from collections.abc import Awaitable, Callable

from app.models import Book


UNKNOWN_TITLES = {"未知書名", "unknown", "unkown"}
UNKNOWN_AUTHORS = {"未知作者", "unknown", "unkown"}
UNKNOWN_CATEGORIES = {"未分類", "unknown", "unkown"}


def _is_unknown(value: str | None, unknown_values: set[str]) -> bool:
    normalized = str(value or "").strip().casefold()
    return not normalized or normalized in {
        item.casefold() for item in unknown_values
    }


def book_metadata_is_incomplete(book: Book) -> bool:
    return (
        _is_unknown(book.title, UNKNOWN_TITLES)
        or _is_unknown(book.author, UNKNOWN_AUTHORS)
        or _is_unknown(book.category, UNKNOWN_CATEGORIES)
        or not str(book.cover_url or "").strip()
    )


async def refresh_incomplete_book_metadata(
    book: Book,
    *,
    isbn: str,
    raw_title: str,
    crawler_cover: str | None,
    fetch_metadata: Callable[..., Awaitable[dict]],
) -> bool:
    """Fill missing fields without replacing useful metadata."""
    before = (book.title, book.author, book.category, book.cover_url)

    if not str(book.cover_url or "").strip() and crawler_cover:
        book.cover_url = crawler_cover

    metadata: dict = {}
    if book_metadata_is_incomplete(book):
        metadata = await fetch_metadata(isbn=isbn, raw_title=raw_title)

    if _is_unknown(book.title, UNKNOWN_TITLES):
        book.title = metadata.get("title") or raw_title or book.title
    if _is_unknown(book.author, UNKNOWN_AUTHORS):
        book.author = metadata.get("author") or book.author
    if _is_unknown(book.category, UNKNOWN_CATEGORIES):
        book.category = (
            metadata.get("standard_category")
            or metadata.get("category")
            or book.category
        )
    if not str(book.cover_url or "").strip():
        book.cover_url = metadata.get("cover_url") or book.cover_url

    after = (book.title, book.author, book.category, book.cover_url)
    return after != before
