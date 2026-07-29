import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import auth, readmoo_replication
from app.services import platform_auth
from app.services.kobo_library_worker import _canonical_isbn_by_platform_id
from app.services.wishlist_reconciliation import (
    deduplicate_remote_books,
    remove_stale_synced_wishlist_items,
)
from app.api.wishlist import (
    WishlistCreate,
    WishlistTransfer,
    add_to_wishlist,
    get_wishlist,
    trigger_wishlist_import,
    transfer_to_library,
)
from app.models import Book, PlatformSession, Purchase, WishlistItem


class WishlistApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def test_add_by_title_creates_book_and_two_platform_items(self):
        metadata = {
            "source": "readmoo",
            "title": "測試書名",
            "author": "測試作者",
            "category": "文學小說",
            "cover_url": "https://example.test/cover.jpg",
            "identifiers": ["9789571078304"],
        }
        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                AsyncMock(return_value=metadata),
            ),
        ):
            result = asyncio.run(add_to_wishlist(
                WishlistCreate(user_id="reader", query="測試書名"),
                BackgroundTasks(),
                session,
            ))
            items = session.exec(select(WishlistItem)).all()
            book = session.get(Book, "9789571078304")

        self.assertEqual(result["book"]["isbn"], "9789571078304")
        self.assertEqual(book.title, "測試書名")
        self.assertEqual(
            {item.platform for item in items},
            {"kobo", "readmoo"},
        )

    def test_wishlist_is_grouped_into_book_cards(self):
        with Session(self.engine) as session:
            session.add(Book(
                isbn="book-a",
                title="同一本書",
                author="作者",
                category="文學小說",
            ))
            session.add_all([
                WishlistItem(
                    user_id="reader",
                    isbn="book-a",
                    platform="kobo",
                    sync_status="pending",
                ),
                WishlistItem(
                    user_id="reader",
                    isbn="book-a",
                    platform="readmoo",
                    sync_status="synced",
                ),
            ])
            session.commit()
            result = asyncio.run(get_wishlist(
                user_id="reader",
                session=session,
            ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "同一本書")
        self.assertEqual(len(result[0]["platforms"]), 2)

    def test_remote_import_removes_only_stale_synced_platform_items(self):
        with Session(self.engine) as session:
            session.add_all([
                Book(isbn="gone", title="已從遠端刪除", category="未分類"),
                Book(isbn="keep", title="仍在遠端", category="未分類"),
                Book(isbn="pending", title="等待同步", category="未分類"),
            ])
            session.add_all([
                WishlistItem(
                    user_id="reader", isbn="gone", platform="kobo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader", isbn="keep", platform="kobo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader", isbn="pending", platform="kobo",
                    sync_status="pending",
                ),
                WishlistItem(
                    user_id="reader", isbn="gone", platform="readmoo",
                    sync_status="synced",
                ),
            ])
            session.commit()

            removed = remove_stale_synced_wishlist_items(
                session,
                "reader",
                "kobo",
                [{"isbn": "keep", "title": "仍在遠端"}],
            )
            session.commit()
            items = session.exec(select(WishlistItem)).all()

        self.assertEqual(removed, 1)
        self.assertEqual(
            {(item.isbn, item.platform) for item in items},
            {("keep", "kobo"), ("pending", "kobo"), ("gone", "readmoo")},
        )

    def test_remote_import_deduplicates_equivalent_isbn_formats(self):
        books = deduplicate_remote_books(
            [
                {"isbn": "978-957-10-7830-4", "title": "第一筆"},
                {"isbn": " 9789571078304 ", "title": "重複資料"},
            ],
            "kobo",
        )

        self.assertEqual(
            books,
            [{"isbn": "9789571078304", "title": "第一筆"}],
        )

    def test_missing_remote_ids_get_distinct_stable_keys(self):
        books = deduplicate_remote_books(
            [
                {"isbn": "UNKNOWN_ISBN", "title": "甲書"},
                {"isbn": None, "title": "乙書"},
                {"isbn": "", "title": " 甲書 "},
            ],
            "readmoo",
        )

        self.assertEqual(len(books), 2)
        self.assertNotEqual(books[0]["isbn"], books[1]["isbn"])
        self.assertTrue(books[0]["isbn"].startswith("readmoo:title:"))
        self.assertEqual(
            books,
            deduplicate_remote_books(
                [
                    {"isbn": None, "title": "甲書"},
                    {"isbn": "UNKNOWN_ISBN", "title": "乙書"},
                ],
                "readmoo",
            ),
        )

    def test_kobo_platform_id_resolves_to_existing_canonical_isbn(self):
        purchases = [
            Purchase(
                user_id="reader",
                platform="kobo",
                platform_book_id="kobo-product-uuid",
                isbn="9786263901438",
            ),
        ]

        self.assertEqual(
            _canonical_isbn_by_platform_id(purchases),
            {"kobo-product-uuid": "9786263901438"},
        )

    def test_local_readmoo_snapshot_upserts_without_cookie_data(self):
        payload = readmoo_replication.ReadmooSnapshotPayload(
            user_id="reader",
            books=[
                readmoo_replication.ReadmooBookPayload(
                    isbn="owned-book",
                    title="本機書櫃書籍",
                    author="本機作者",
                    category="文學小說",
                    platform_book_id="readmoo-product-1",
                ),
            ],
            wishlist_synced=True,
            wishlist=[
                readmoo_replication.ReadmooBookPayload(
                    isbn="wanted-book",
                    title="本機待購書籍",
                    category="人文社科",
                ),
            ],
        )
        with Session(self.engine) as session:
            session.add_all([
                Book(isbn="stale-book", title="舊待購", category="未分類"),
                WishlistItem(
                    user_id="reader",
                    isbn="stale-book",
                    platform="readmoo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader",
                    isbn="stale-book",
                    platform="kobo",
                    sync_status="synced",
                ),
            ])
            session.commit()
            with patch.object(readmoo_replication, "set_platform_session_status"):
                result = readmoo_replication.apply_readmoo_snapshot(
                    session,
                    payload,
                )
            purchases = session.exec(select(Purchase)).all()
            wish_items = session.exec(select(WishlistItem)).all()

        self.assertEqual(result["purchases_added"], 1)
        self.assertEqual(result["wishlist_removed"], 1)
        self.assertEqual(
            {(purchase.isbn, purchase.platform) for purchase in purchases},
            {("owned-book", "readmoo")},
        )
        self.assertEqual(
            {(item.isbn, item.platform) for item in wish_items},
            {("wanted-book", "readmoo"), ("stale-book", "kobo")},
        )

    def test_add_by_title_refines_missing_fields_with_resolved_isbn(self):
        first_match = {
            "source": "readmoo",
            "title": "待補資料書籍",
            "author": "未知作者",
            "category": "未分類",
            "standard_category": "未分類",
            "identifiers": ["9789571078304"],
        }
        exact_match = {
            "source": "ncl",
            "isbn": "9789571078304",
            "isbn_valid": True,
            "title": "待補資料書籍",
            "author": "完整作者",
            "category": "文學小說",
            "standard_category": "文學小說",
            "identifiers": ["9789571078304"],
        }
        metadata_lookup = AsyncMock(
            side_effect=[first_match, exact_match],
        )

        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                metadata_lookup,
            ),
        ):
            result = asyncio.run(add_to_wishlist(
                WishlistCreate(user_id="reader", query="待補資料書籍"),
                BackgroundTasks(),
                session,
            ))

        self.assertEqual(metadata_lookup.await_count, 2)
        self.assertEqual(result["book"]["author"], "完整作者")
        self.assertEqual(result["book"]["category"], "文學小說")

    def test_get_wishlist_enriches_title_only_imported_book(self):
        metadata = {
            "source": "readmoo",
            "isbn": "",
            "isbn_valid": False,
            "title": "遠端匯入書籍",
            "author": "遠端作者",
            "category": "人文社科",
            "standard_category": "人文社科",
            "cover_url": "https://example.test/imported.jpg",
        }
        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                AsyncMock(return_value=metadata),
            ),
        ):
            session.add(Book(
                isbn="remote-platform-id",
                title="遠端匯入書籍",
                author="未知作者",
                category="未分類",
            ))
            session.add(WishlistItem(
                user_id="reader",
                isbn="remote-platform-id",
                platform="readmoo",
                sync_status="synced",
            ))
            session.commit()

            result = asyncio.run(get_wishlist(
                user_id="reader",
                session=session,
            ))

        self.assertEqual(result[0]["author"], "遠端作者")
        self.assertEqual(result[0]["category"], "人文社科")
        self.assertEqual(
            result[0]["cover_url"],
            "https://example.test/imported.jpg",
        )

    def test_bulk_transfer_rejects_two_platforms(self):
        with Session(self.engine) as session:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(transfer_to_library(
                    WishlistTransfer(
                        user_id="reader",
                        isbns=["book-a", "book-b"],
                        platforms=["kobo", "readmoo"],
                    ),
                    BackgroundTasks(),
                    session,
                ))
        self.assertEqual(raised.exception.status_code, 400)

    def test_bulk_transfer_to_one_platform_creates_purchases(self):
        with Session(self.engine) as session:
            for isbn in ("book-a", "book-b"):
                session.add(Book(
                    isbn=isbn,
                    title=isbn,
                    category="文學小說",
                ))
                session.add(WishlistItem(
                    user_id="reader",
                    isbn=isbn,
                    platform="readmoo",
                    sync_status="synced",
                ))
            session.commit()

            asyncio.run(transfer_to_library(
                WishlistTransfer(
                    user_id="reader",
                    isbns=["book-a", "book-b"],
                    platforms=["kobo"],
                ),
                BackgroundTasks(),
                session,
            ))
            purchases = session.exec(select(Purchase)).all()
            wishlist_items = session.exec(select(WishlistItem)).all()

        self.assertEqual(
            {(row.isbn, row.platform) for row in purchases},
            {("book-a", "kobo"), ("book-b", "kobo")},
        )
        self.assertEqual(wishlist_items, [])


class PlatformStatusTests(unittest.TestCase):
    def test_missing_state_requires_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "kobo",
                    None,
                )

        self.assertEqual(status["status"], "missing")
        self.assertTrue(status["needs_update"])

    def test_session_cookie_requires_recent_active_verification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "readmoo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                (
                    '{"cookies":[{"name":"oauth_token","value":"ok",'
                    '"domain":"read.readmoo.com","expires":-1}]}'
                ),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                active = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="active",
                        updated_at=datetime.utcnow(),
                    ),
                )
                unverified = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    None,
                )
                expired = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="expired",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(active["status"], "active")
        self.assertFalse(active["needs_update"])
        self.assertEqual(unverified["status"], "unverified")
        self.assertTrue(unverified["needs_update"])
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(expired["needs_update"])

    def test_blocked_session_is_not_reported_as_active(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "readmoo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                ('{"cookies":[{"name":"oauth_token","value":"ok",'
                 '"domain":"read.readmoo.com","expires":-1}]}'),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="blocked",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(status["status"], "blocked")
        self.assertTrue(status["needs_update"])

    def test_remote_readmoo_sync_is_not_reported_as_vps_login(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="remote_synced",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(status["status"], "remote_synced")
        self.assertFalse(status["needs_update"])

    def test_tracking_cookies_do_not_count_as_platform_login(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "kobo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                (
                    '{"cookies":[{"name":"_ga","value":"tracking",'
                    '"domain":".kobo.com","expires":-1}]}'
                ),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "kobo",
                    None,
                )

        self.assertEqual(status["status"], "expired")
        self.assertTrue(status["needs_update"])

    def test_login_endpoint_returns_saved_cookie_result(self):
        expected = {
            "status": "success",
            "platform": "readmoo",
            "cookie_count": 3,
            "message": "readmoo 登入憑證已更新",
        }
        with patch.object(
            auth,
            "login_and_save_platform_state",
            AsyncMock(return_value=expected),
        ):
            result = asyncio.run(auth.login_platform("reader", "readmoo"))

        self.assertEqual(result, expected)

    def test_readmoo_callback_must_finish_before_login_redirects(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

        self.assertFalse(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://www.readmoo.com/?key=true"),
        ))
        self.assertTrue(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://www.readmoo.com/"),
        ))
        self.assertTrue(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://read.readmoo.com/#/dashboard"),
        ))

    def test_readmoo_uses_bundled_chromium_by_default(self):
        chromium = unittest.mock.Mock()
        chromium.launch = AsyncMock(return_value="browser")
        playwright = unittest.mock.Mock(chromium=chromium)

        with (
            patch.object(platform_auth, "READMOO_BROWSER_CHANNEL", ""),
            patch.object(platform_auth, "READMOO_BROWSER_PROXY", ""),
        ):
            browser = asyncio.run(platform_auth.launch_readmoo_browser(
                playwright,
                headless=False,
            ))

        self.assertEqual(browser, "browser")
        chromium.launch.assert_awaited_once_with(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

    def test_readmoo_can_use_installed_chrome_channel(self):
        chromium = unittest.mock.Mock()
        chromium.launch = AsyncMock(return_value="browser")
        playwright = unittest.mock.Mock(chromium=chromium)

        with (
            patch.object(platform_auth, "READMOO_BROWSER_CHANNEL", "chrome"),
            patch.object(
                platform_auth,
                "READMOO_BROWSER_PROXY",
                "socks5://readmoo-vpn:1080",
            ),
        ):
            asyncio.run(platform_auth.launch_readmoo_browser(
                playwright,
                headless=False,
            ))

        chromium.launch.assert_awaited_once_with(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": "socks5://readmoo-vpn:1080"},
        )

    def test_login_endpoint_returns_403_when_readmoo_is_waf_blocked(self):
        with patch.object(
            auth,
            "login_and_save_platform_state",
            AsyncMock(side_effect=platform_auth.PlatformLoginBlocked("blocked")),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(auth.login_platform("reader", "readmoo"))

        self.assertEqual(raised.exception.status_code, 403)

    def test_wishlist_import_returns_platform_outcomes(self):
        with (
            patch(
                "app.api.wishlist.import_readmoo_wishlist_to_db",
                AsyncMock(return_value={
                    "platform": "readmoo",
                    "status": "blocked",
                    "books": 0,
                    "message": "blocked",
                }),
            ),
            patch(
                "app.api.wishlist.import_kobo_wishlist_to_db",
                AsyncMock(return_value={
                    "platform": "kobo",
                    "status": "success",
                    "books": 2,
                    "message": "ok",
                }),
            ),
        ):
            result = asyncio.run(trigger_wishlist_import("reader"))

        self.assertEqual(result["statuses"], {
            "readmoo": "blocked",
            "kobo": "success",
        })
        self.assertEqual(result["blocked"], ["readmoo"])

    def test_saved_state_excludes_unrelated_oauth_cookies(self):
        class FakeContext:
            async def storage_state(self):
                return {
                    "cookies": [
                        {
                            "name": "oauth_token",
                            "domain": "read.readmoo.com",
                            "value": "book-session",
                        },
                        {
                            "name": "SID",
                            "domain": ".google.com",
                            "value": "unrelated",
                        },
                    ],
                    "origins": [],
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            state = asyncio.run(platform_auth.save_platform_storage_state(
                FakeContext(),
                state_path,
                "readmoo",
            ))

        self.assertEqual(len(state["cookies"]), 1)
        self.assertEqual(state["cookies"][0]["name"], "oauth_token")


class LibraryImportResultTests(unittest.TestCase):
    def test_auth_required_platform_is_returned_to_frontend(self):
        from main import import_library

        with (
            patch(
                "main.import_readmoo_library_to_db",
                AsyncMock(return_value={
                    "platform": "readmoo",
                    "status": "auth_required",
                    "message": "Readmoo 登入憑證已失效",
                    "new_books": 0,
                }),
            ),
            patch(
                "main.import_kobo_library_to_db",
                AsyncMock(return_value={
                    "platform": "kobo",
                    "status": "success",
                    "message": "Kobo 書櫃同步完成",
                    "new_books": 1,
                }),
            ),
        ):
            result = asyncio.run(import_library("reader", limit=None))

        self.assertEqual(result["status"], "auth_required")
        self.assertEqual(result["needs_auth"], ["readmoo"])
        self.assertEqual(len(result["results"]), 2)


if __name__ == "__main__":
    unittest.main()
