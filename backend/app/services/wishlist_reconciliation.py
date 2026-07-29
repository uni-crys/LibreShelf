"""Safe reconciliation of locally cached platform wishlists."""

import hashlib
import re

from sqlmodel import Session, select

from app.models import Book, WishlistItem

_MISSING_IDENTIFIERS = {"", "none", "null", "unknown", "unknown_isbn", "undefined"}


def _normalized_title(value: str | None) -> str:
    """Use titles as a fallback because Kobo ProductId is not always an ISBN."""
    return "".join(
        character.casefold()
        for character in (value or "")
        if character.isalnum()
    )


def _normalized_identifier(value: object) -> str:
    identifier = str(value or "").strip()
    if identifier.casefold() in _MISSING_IDENTIFIERS:
        return ""

    # ISBNs are commonly returned with spaces or hyphens by one endpoint and
    # without them by another.  Only collapse separators when the result looks
    # like an ISBN; platform product IDs must otherwise remain untouched.
    compact = re.sub(r"[\s-]", "", identifier).upper()
    if re.fullmatch(r"(?:\d{9}[\dX]|\d{13})", compact):
        return compact
    return identifier


def deduplicate_remote_books(
    remote_books: list[dict],
    platform: str,
) -> list[dict]:
    """Return one stable record per remote book.

    A missing product ID must never become the shared ``UNKNOWN_ISBN`` primary
    key.  Use a deterministic, platform-scoped title key instead so unrelated
    books cannot overwrite each other on import.
    """
    deduplicated: list[dict] = []
    seen_identifiers: set[str] = set()
    seen_fallback_titles: set[str] = set()

    for book in remote_books:
        title = str(book.get("title") or "").strip()
        if not title:
            continue

        identifier = _normalized_identifier(book.get("isbn"))
        normalized_title = _normalized_title(title)
        if identifier:
            if identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)
        else:
            if not normalized_title or normalized_title in seen_fallback_titles:
                continue
            seen_fallback_titles.add(normalized_title)
            digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
            identifier = f"{platform}:title:{digest}"

        deduplicated.append({**book, "isbn": identifier, "title": title})

    return deduplicated


def remove_stale_synced_wishlist_items(
    db: Session,
    user_id: str,
    platform: str,
    remote_books: list[dict],
) -> int:
    """Remove only confirmed-synced items absent from a successful remote import.

    Pending/failed items are deliberately retained: they may be waiting for an
    add/remove action and must not disappear merely because a remote import ran.
    """
    remote_identifiers = {
        str(book.get("isbn") or "").strip()
        for book in remote_books
    }
    remote_titles = {
        _normalized_title(str(book.get("title") or ""))
        for book in remote_books
    }
    remote_titles.discard("")

    synced_items = db.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == user_id,
            WishlistItem.platform == platform,
            WishlistItem.sync_status == "synced",
        )
    ).all()

    removed = 0
    for item in synced_items:
        book = db.get(Book, item.isbn)
        title = _normalized_title(book.title if book else "")
        if item.isbn in remote_identifiers or (title and title in remote_titles):
            continue
        db.delete(item)
        removed += 1
    return removed
