import os
from pathlib import Path
from playwright.async_api import async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import Book, Purchase
from app.services.library_metadata import refresh_incomplete_book_metadata
from app.services.library_navigation import (
    is_readmoo_dashboard_url,
    is_readmoo_library_url,
    wait_for_stable_route,
)
from app.services.wishlist_reconciliation import deduplicate_remote_books
from app.services.metadata_pipeline import (
    fetch_and_clean_metadata,
    is_valid_isbn,
    normalize_text,
)
from app.services.platform_auth import (
    get_platform_auth_cookies,
    get_platform_state_path,
    launch_readmoo_browser,
    save_platform_storage_state,
    set_platform_session_status,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "False").lower() == "true"


async def _first_visible(page, selectors: tuple[str, ...]):
    """Select by semantic priority instead of document order."""
    for selector in selectors:
        matches = page.locator(selector)
        for index in range(await matches.count()):
            locator = matches.nth(index)
            if await locator.is_visible():
                return locator
    return None


def _canonical_isbn_by_platform_id(
    purchases: list[Purchase],
) -> dict[str, str]:
    return {
        str(purchase.platform_book_id).strip(): purchase.isbn
        for purchase in purchases
        if str(purchase.platform_book_id or "").strip()
    }


def _metadata_matches_platform_title(
    raw_title: str,
    metadata_title: str | None,
) -> bool:
    """Reject ambiguous expansion such as 鯨 -> 鯨滅 or 大橋 -> 大橋驟雨."""
    raw = normalize_text(raw_title)
    candidate = normalize_text(metadata_title)
    if not raw or not candidate:
        return False
    if raw == candidate:
        return True
    return len(raw) >= 4 and (raw in candidate or candidate in raw)


def get_user_state_path(user_id: str) -> Path:
    return get_platform_state_path(user_id, "readmoo")

async def import_readmoo_library_to_db(user_id: str, limit: int | None = None):
    effective_limit = limit if limit is not None and limit > 0 else None
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        set_platform_session_status(user_id, "readmoo", "expired")
        return {
            "platform": "readmoo",
            "status": "auth_required",
            "message": "找不到 Readmoo 登入憑證",
            "new_books": 0,
        }
    
    async with async_playwright() as p:
        browser = await launch_readmoo_browser(p, headless=IS_HEADLESS)
        
        context_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "storage_state": str(state_file_path),
        }
            
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        remote_books = []
        new_books_count = 0
        updated_books_count = 0

        try:
            print(
                "[Readmoo Library Import] 開始同步 Readmoo 已購書櫃..."
                + (
                    f"（測試模式：最多 {effective_limit} 本）"
                    if effective_limit is not None
                    else ""
                )
            )
            
            # 1. 前往閱讀器首頁 / 總覽
            await page.goto("https://read.readmoo.com/#/dashboard", wait_until="domcontentloaded", timeout=30000)
            dashboard_status = await wait_for_stable_route(
                page,
                is_readmoo_dashboard_url,
                timeout_ms=180000,
            )
            if dashboard_status == "blocked":
                set_platform_session_status(user_id, "readmoo", "blocked")
                return {
                    "platform": "readmoo",
                    "status": "blocked",
                    "message": "Readmoo 要求完成人機驗證，已停止同步",
                    "new_books": 0,
                }
            if dashboard_status != "ready":
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 總覽頁仍在重載，已停止同步",
                    "new_books": 0,
                }

            # 2. 自動偵測登入
            current_url = page.url.lower()
            auth_cookies = get_platform_auth_cookies(
                await context.cookies(),
                "readmoo",
            )
            login_visible = await page.locator(
                "a:has-text('登入'):visible, button:has-text('登入'):visible"
            ).count()
            if (
                any(
                    token in current_url
                    for token in ("signin", "login", "oauth2")
                )
                or login_visible > 0
                or not auth_cookies
            ):
                print(
                    f"[Readmoo Library Import] 使用者 {user_id} "
                    "的登入憑證已失效"
                )
                set_platform_session_status(user_id, "readmoo", "expired")
                return {
                    "platform": "readmoo",
                    "status": "auth_required",
                    "message": "Readmoo 登入憑證已失效",
                    "new_books": 0,
                }

            set_platform_session_status(user_id, "readmoo", "active")

            await page.wait_for_timeout(2000)

            # 3. 展開「書櫃」accordion。本按鈕只展開選單，不改 URL。
            print("[Readmoo Library Import] 正在確認「書櫃」選單...")
            try:
                bookcase_button = await _first_visible(
                    page,
                    (
                        "button.accordion-button:has-text('書櫃')",
                        "button[aria-expanded]:has-text('書櫃')",
                    ),
                )
                if bookcase_button is None:
                    print(
                        "[Readmoo Library Import] 找不到可見的書櫃入口，"
                        f"目前 URL: {page.url}"
                    )
                    set_platform_session_status(user_id, "readmoo", "parser_error")
                    return {
                        "platform": "readmoo",
                        "status": "parser_error",
                        "message": "Readmoo 總覽頁找不到書櫃入口",
                        "new_books": 0,
                    }
                expanded = (
                    await bookcase_button.get_attribute("aria-expanded")
                ) == "true"
                if not expanded:
                    await bookcase_button.click()
                    await page.wait_for_timeout(500)
                    print("[Readmoo Library Import] ✅ 已展開「書櫃」選單")
                else:
                    print("[Readmoo Library Import] 「書櫃」選單已展開")
            except Exception as e:
                print(f"[Readmoo Library Import] 展開書櫃選單失敗: {e}")
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 無法展開書櫃選單",
                    "new_books": 0,
                }

            # 4. 點擊 accordion 內真正導航到 #/library 的「書籍」連結。
            print("[Readmoo Library Import] 正在點擊「書籍」...")
            try:
                books_btn = await _first_visible(
                    page,
                    (
                        "a[href='#/library']:has-text('書籍')",
                        "a[href='/#/library']:has-text('書籍')",
                        "[role='tab']:has-text('書籍')",
                    ),
                )
                if books_btn is not None:
                    await books_btn.click()
                    print("[Readmoo Library Import] ✅ 已點擊「書籍」，等待 #/library")
                else:
                    print(
                        "[Readmoo Library Import] 書櫃選單內找不到"
                        f"可見的「書籍」分類，目前 URL: {page.url}"
                    )
                    set_platform_session_status(user_id, "readmoo", "parser_error")
                    return {
                        "platform": "readmoo",
                        "status": "parser_error",
                        "message": "Readmoo 書櫃找不到「書籍」分類",
                        "new_books": 0,
                    }
            except Exception as e:
                print(f"[Readmoo Library Import] 切換書籍分類失敗: {e}")
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 無法切換至「書籍」分類",
                    "new_books": 0,
                }

            library_status = await wait_for_stable_route(
                page,
                is_readmoo_library_url,
            )
            if library_status != "ready":
                print(
                    "[Readmoo Library Import] 點擊「書籍」後未穩定進入"
                    f" #/library，狀態: {library_status}，目前 URL: {page.url}"
                )
                status = "blocked" if library_status == "blocked" else "parser_error"
                set_platform_session_status(user_id, "readmoo", status)
                return {
                    "platform": "readmoo",
                    "status": status,
                    "message": "Readmoo 尚未穩定進入書籍頁面，已停止同步",
                    "new_books": 0,
                }
            print("[Readmoo Library Import] ✅ 已穩定進入 #/library 書籍頁面")

            # 5. 展開全部書籍
            print(f"[Readmoo Library Import] 正在展開並載入所有書籍...")
            for _ in range(30):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                await page.wait_for_timeout(1000)

                more_btn = page.locator("button:has-text('更多...')").first
                if await more_btn.count() > 0 and await more_btn.is_visible():
                    try:
                        await more_btn.click()
                        print(f"[Readmoo Library Import] 已點擊「更多...」載入更多書籍...")
                        await page.wait_for_timeout(1500)
                    except Exception:
                        break
                else:
                    break

            # 6. 解析書本區塊
            book_items = page.locator(".library-item")
            count = await book_items.count()
            print(f"[Readmoo Library Import] 展開完成，實際找到書本區塊數量: {count}")
            if count == 0:
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 書籍清單尚未完成載入",
                    "new_books": 0,
                }

            for i in range(count):
                item = book_items.nth(i)
                try:
                    title_el = item.locator(".title").first
                    title = ""
                    if await title_el.count() > 0:
                        title = await title_el.get_attribute("title")
                        if not title:
                            title = await title_el.inner_text()

                    img_el = item.locator("img.cover-img").first
                    cover_url = await img_el.get_attribute("src") if await img_el.count() > 0 else ""

                    reader_link = item.locator("a.reader-link").first
                    href = await reader_link.get_attribute("href") if await reader_link.count() > 0 else ""
                    
                    privacy_div = item.locator("div[id^='privacy-']").first
                    privacy_id = ""
                    if await privacy_div.count() > 0:
                        privacy_id = await privacy_div.get_attribute("id")

                    isbn = privacy_id.replace("privacy-", "") if privacy_id else (href.split("/")[-1] if href else "")

                    if title and title.strip():
                        remote_books.append({
                            "isbn": str(isbn).strip(),
                            "title": title.strip(),
                            "cover_url": cover_url.strip() if cover_url else None
                        })
                except Exception as e:
                    print(f"[Readmoo Library Import] 解析第 {i} 本書失敗: {e}")

            remote_books = deduplicate_remote_books(remote_books, "readmoo")
            print(f"[Readmoo Library Import] 成功解析 {len(remote_books)} 本已購書籍")

            # 7. 更新憑證
            if len(remote_books) > 0:
                state_file_path.parent.mkdir(parents=True, exist_ok=True)
                await save_platform_storage_state(
                    context,
                    state_file_path,
                    "readmoo",
                )
                print(f"[Readmoo Library Import] 💡 已完美自動更新最新憑證至 state.json！")

                # 8. 寫入 DB (透過單一 Session 集中處理)
                with Session(engine) as db:
                    existing_purchases = db.exec(
                        select(Purchase).where(
                            Purchase.user_id == user_id,
                            Purchase.platform == "readmoo"
                        )
                    ).all()
                    existing_isbns = {
                        purchase.isbn for purchase in existing_purchases
                    }
                    isbn_by_platform_id = _canonical_isbn_by_platform_id(
                        existing_purchases
                    )

                    for b_info in remote_books:
                        platform_book_id = b_info["isbn"]
                        isbn = isbn_by_platform_id.get(
                            platform_book_id,
                            platform_book_id,
                        )
                        raw_title = b_info["title"]
                        crawler_cover = b_info["cover_url"]

                        if isbn in existing_isbns:
                            book = db.get(Book, isbn)
                            if book and await refresh_incomplete_book_metadata(
                                book,
                                isbn=isbn,
                                raw_title=raw_title,
                                crawler_cover=crawler_cover,
                                fetch_metadata=fetch_and_clean_metadata,
                            ):
                                db.add(book)
                                updated_books_count += 1
                            continue
                        if (
                            effective_limit is not None
                            and new_books_count >= effective_limit
                        ):
                            print(
                                f"[Readmoo Library Import] 已達新書測試上限 "
                                f"{effective_limit} 本"
                            )
                            break

                        new_books_count += 1

                        # 帶入 raw_title，針對 Readmoo 8 碼 ID 改以書名向博客來搜尋作者與分類
                        meta = await fetch_and_clean_metadata(isbn=isbn, raw_title=raw_title)
                        metadata_reliable = (
                            is_valid_isbn(isbn)
                            or _metadata_matches_platform_title(
                                raw_title,
                                meta.get("title"),
                            )
                        )

                        book = db.get(Book, isbn)
                        if not book:
                            book = Book(
                                isbn=isbn,
                                title=(
                                    meta.get("title")
                                    if metadata_reliable
                                    else raw_title
                                ) or raw_title,
                                author=(
                                    meta.get("author")
                                    if metadata_reliable
                                    else None
                                ) or "未知作者",
                                cover_url=(
                                    meta.get("cover_url")
                                    if metadata_reliable
                                    else crawler_cover
                                ) or crawler_cover,
                                category=(
                                    meta.get("category")
                                    if metadata_reliable
                                    else None
                                ) or "未分類"
                            )
                            db.add(book)
                        else:
                            if book.author == "未知作者" or not book.author:
                                book.author = meta.get("author") or "未知作者"
                            if book.category == "未分類" or not book.category:
                                book.category = meta.get("category") or "未分類"
                            if not book.cover_url and crawler_cover:
                                book.cover_url = crawler_cover
                            db.add(book)

                        db.add(Purchase(
                            user_id=user_id,
                            platform="readmoo",
                            platform_book_id=platform_book_id,
                            isbn=isbn
                        ))

                    # 集中單次 commit，避免 database is locked
                    db.commit()
                print(
                    "[Readmoo Library Import] 同步完成！"
                    f"新增 {new_books_count} 本，補齊 {updated_books_count} 本"
                )

            return {
                "platform": "readmoo",
                "status": "success",
                "message": "Readmoo 書櫃同步完成",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
                "remote_books": len(remote_books),
            }
        except Exception as e:
            print(f"[Readmoo Library Import] 同步過程發生錯誤: {e}")
            return {
                "platform": "readmoo",
                "status": "failed",
                "message": str(e),
                "new_books": new_books_count,
                "updated_books": updated_books_count,
            }
        finally:
            await browser.close()
