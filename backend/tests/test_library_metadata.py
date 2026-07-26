import asyncio
import unittest
from unittest.mock import AsyncMock

from app.models import Book
from app.services.library_metadata import (
    book_metadata_is_incomplete,
    refresh_incomplete_book_metadata,
)


class LibraryMetadataTests(unittest.TestCase):
    def test_complete_book_does_not_need_refresh(self):
        book = Book(
            isbn="complete",
            title="完整書籍",
            author="作者",
            category="文學小說",
            cover_url="https://example.com/cover.jpg",
        )

        self.assertFalse(book_metadata_is_incomplete(book))

    def test_unknown_category_is_incomplete(self):
        book = Book(
            isbn="unknown-category",
            title="待補分類",
            author="作者",
            category="未分類",
            cover_url="https://example.com/cover.jpg",
        )

        self.assertTrue(book_metadata_is_incomplete(book))

    def test_refresh_fills_missing_fields_without_overwriting_known_values(self):
        book = Book(
            isbn="book-id",
            title="原始正確書名",
            author="未知作者",
            category="未分類",
            cover_url=None,
        )
        fetch_metadata = AsyncMock(return_value={
            "title": "API 書名",
            "author": "補回作者",
            "category": "人文社科",
            "standard_category": "人文社科",
            "cover_url": "https://example.com/api-cover.jpg",
        })

        changed = asyncio.run(refresh_incomplete_book_metadata(
            book,
            isbn="book-id",
            raw_title="書櫃書名",
            crawler_cover="https://example.com/crawler-cover.jpg",
            fetch_metadata=fetch_metadata,
        ))

        self.assertTrue(changed)
        self.assertEqual(book.title, "原始正確書名")
        self.assertEqual(book.author, "補回作者")
        self.assertEqual(book.category, "人文社科")
        self.assertEqual(
            book.cover_url,
            "https://example.com/crawler-cover.jpg",
        )
        fetch_metadata.assert_awaited_once_with(
            isbn="book-id",
            raw_title="書櫃書名",
        )

    def test_crawler_cover_repairs_cover_without_api_lookup(self):
        book = Book(
            isbn="cover-only",
            title="完整書名",
            author="作者",
            category="文學小說",
            cover_url=None,
        )
        fetch_metadata = AsyncMock()

        changed = asyncio.run(refresh_incomplete_book_metadata(
            book,
            isbn="cover-only",
            raw_title="完整書名",
            crawler_cover="https://example.com/crawler-cover.jpg",
            fetch_metadata=fetch_metadata,
        ))

        self.assertTrue(changed)
        self.assertEqual(
            book.cover_url,
            "https://example.com/crawler-cover.jpg",
        )
        fetch_metadata.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
