import os
import json
from pathlib import Path
from playwright.async_api import async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import Book, Purchase
from app.services.library_metadata import book_metadata_is_incomplete
from app.services.metadata_matching import (
    MetadataMatchAction,
    apply_metadata_decision,
    decide_metadata_match,
    metadata_book_values,
)
from app.services.library_navigation import (
    is_kobo_home_url,
    is_kobo_library_url,
    wait_for_stable_route,
)
from app.services.wishlist_reconciliation import deduplicate_remote_books
from app.services.metadata_pipeline import fetch_and_clean_metadata
from app.services.platform_auth import (
    get_platform_auth_cookies,
    get_platform_state_path,
    save_platform_storage_state,
    set_platform_session_status,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "True").lower() == "true"


def _canonical_isbn_by_platform_id(
    purchases: list[Purchase],
) -> dict[str, str]:
    """Map Kobo's stable product ID to the canonical local book key."""
    return {
        str(purchase.platform_book_id).strip(): purchase.isbn
        for purchase in purchases
        if str(purchase.platform_book_id or "").strip()
    }


def get_user_state_path(user_id: str) -> Path:
    return get_platform_state_path(user_id, "kobo")

async def import_kobo_library_to_db(user_id: str, limit: int | None = None):
    effective_limit = limit if limit is not None and limit > 0 else None
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        print(f"[Kobo Library Import] 找不到使用者 {user_id} 的憑證檔，無法同步已購書櫃")
        set_platform_session_status(user_id, "kobo", "expired")
        return {
            "platform": "kobo",
            "status": "auth_required",
            "message": "找不到 Kobo 登入憑證",
            "new_books": 0,
        }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=IS_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        remote_books = []
        new_books_count = 0
        updated_books_count = 0

        try:
            print(
                "[Kobo Library Import] 開始同步 Kobo 已購書櫃..."
                + (
                    f"（測試模式：最多 {effective_limit} 本）"
                    if effective_limit is not None
                    else ""
                )
            )
            # Establish the authenticated storefront session first.  Jumping
            # directly into /library/books is prone to Kobo bot challenges.
            await page.goto(
                "https://www.kobo.com/tw/zh",
                wait_until="domcontentloaded",
                timeout=40000,
            )
            home_status = await wait_for_stable_route(page, is_kobo_home_url)
            if home_status == "blocked":
                set_platform_session_status(user_id, "kobo", "blocked")
                return {
                    "platform": "kobo",
                    "status": "blocked",
                    "message": "Kobo 要求完成人機驗證，請使用 noVNC 手動勾選",
                    "new_books": 0,
                }
            if home_status != "ready":
                set_platform_session_status(user_id, "kobo", "parser_error")
                return {
                    "platform": "kobo",
                    "status": "parser_error",
                    "message": "Kobo 首頁尚未穩定載入，已停止書櫃同步",
                    "new_books": 0,
                }

            auth_cookies = get_platform_auth_cookies(
                await context.cookies(),
                "kobo",
            )
            login_visible = await page.locator(
                "a:has-text('登入'):visible, button:has-text('登入'):visible"
            ).count()
            if (
                "/signin" in page.url.lower()
                or "/login" in page.url.lower()
                or login_visible > 0
                or not auth_cookies
            ):
                print(
                    f"[Kobo Library Import] 使用者 {user_id} "
                    "的登入憑證已失效"
                )
                set_platform_session_status(user_id, "kobo", "expired")
                return {
                    "platform": "kobo",
                    "status": "auth_required",
                    "message": "Kobo 登入憑證已失效",
                    "new_books": 0,
                }

            set_platform_session_status(user_id, "kobo", "active")

            await page.goto(
                "https://www.kobo.com/tw/zh/library/books",
                wait_until="domcontentloaded",
                timeout=40000,
            )
            library_status = await wait_for_stable_route(
                page,
                is_kobo_library_url,
            )
            if library_status == "blocked":
                set_platform_session_status(user_id, "kobo", "blocked")
                return {
                    "platform": "kobo",
                    "status": "blocked",
                    "message": "Kobo 書櫃觸發人機驗證，請使用 noVNC 手動勾選",
                    "new_books": 0,
                }
            if library_status != "ready":
                set_platform_session_status(user_id, "kobo", "parser_error")
                return {
                    "platform": "kobo",
                    "status": "parser_error",
                    "message": "Kobo 未能穩定進入書櫃，已停止同步",
                    "new_books": 0,
                }

            page_num = 1
            while True:
                print(f"[Kobo Library Import] 正在爬取第 {page_num} 頁...")
                
                try:
                    await page.wait_for_selector("li.item-wrapper.book, .book-item", timeout=15000)
                except Exception:
                    print(f"[Kobo Library Import] 第 {page_num} 頁等待書櫃項目逾時")
                    break

                await page.wait_for_timeout(3000)

                book_items = page.locator("li.item-wrapper.book, .book-item")
                count = await book_items.count()
                print(f"[Kobo Library Import] 第 {page_num} 頁找到 {count} 個書籍區塊")

                if count == 0:
                    break

                for i in range(count):
                    item = book_items.nth(i)
                    try:
                        title_el = item.locator("h2.title a, .title a, a.title").first
                        title = await title_el.inner_text() if await title_el.count() > 0 else ""
                        
                        img_el = item.locator("img.book-image, img.cover-image").first
                        cover_url = await img_el.get_attribute("src") if await img_el.count() > 0 else ""
                        if cover_url and cover_url.startswith("//"):
                            cover_url = "https:" + cover_url

                        track_info_str = await item.get_attribute("data-track-info") or "{}"
                        try:
                            track_data = json.loads(track_info_str)
                        except json.JSONDecodeError:
                            track_data = {}
                        
                        product_id = track_data.get("productId", "UNKNOWN_ID")
                        
                        if product_id == "UNKNOWN_ID":
                            href = await title_el.get_attribute("href") if await title_el.count() > 0 else ""
                            product_id = href.split("/")[-1] if href else ""

                        if title and title.strip():
                            remote_books.append({
                                "isbn": str(product_id).strip(),
                                "title": title.strip(),
                                "cover_url": cover_url.strip() if cover_url else None
                            })
                    except Exception as e:
                        print(f"[Kobo Library Import] 解析書籍失敗: {e}")

                next_btn = page.locator("a.next, .pagination .next, a[rel='next']").first
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    parent_class = await next_btn.locator("..").get_attribute("class") or ""
                    next_class = await next_btn.get_attribute("class") or ""
                    if "disabled" in parent_class or "disabled" in next_class:
                        break
                    
                    print(f"[Kobo Library Import] 準備前往下一頁...")
                    await next_btn.click()
                    page_num += 1
                    await page.wait_for_timeout(4000)
                else:
                    print(f"[Kobo Library Import] 沒有找到下一頁按鈕，爬取結束。")
                    break

            remote_books = deduplicate_remote_books(remote_books, "kobo")
            print(f"[Kobo Library Import] 總共成功解析 {len(remote_books)} 本已購書籍")

            if len(remote_books) > 0:
                with Session(engine) as db:
                    existing_purchases = db.exec(
                        select(Purchase).where(
                            Purchase.user_id == user_id,
                            Purchase.platform == "kobo"
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
                            if book and book_metadata_is_incomplete(book):
                                meta = await fetch_and_clean_metadata(
                                    isbn=isbn,
                                    raw_title=raw_title,
                                )
                                decision = decide_metadata_match(
                                    identifier=isbn,
                                    raw_title=raw_title,
                                    metadata=meta,
                                )
                                if apply_metadata_decision(
                                    book,
                                    decision,
                                    raw_title=raw_title,
                                    crawler_cover=crawler_cover,
                                    metadata=meta,
                                ):
                                    db.add(book)
                                    updated_books_count += 1
                            continue
                        if (
                            effective_limit is not None
                            and new_books_count >= effective_limit
                        ):
                            print(
                                f"[Kobo Library Import] 已達新書測試上限 "
                                f"{effective_limit} 本"
                            )
                            break

                        # 經由 Pipeline 抓取 metadata (非真 ISBN 時以 raw_title 補齊)
                        meta = await fetch_and_clean_metadata(isbn=isbn, raw_title=raw_title)
                        decision = decide_metadata_match(
                            identifier=isbn,
                            raw_title=raw_title,
                            metadata=meta,
                        )
                        target_isbn = (
                            decision.canonical_isbn
                            if (
                                decision.action
                                == MetadataMatchAction.CANONICALIZE
                                and decision.canonical_isbn
                            )
                            else isbn
                        )

                        if target_isbn in existing_isbns:
                            purchase = next(
                                item for item in existing_purchases
                                if item.isbn == target_isbn
                            )
                            purchase.platform_book_id = platform_book_id
                            db.add(purchase)
                            book = db.get(Book, target_isbn)
                            if book and apply_metadata_decision(
                                book,
                                decision,
                                raw_title=raw_title,
                                crawler_cover=crawler_cover,
                                metadata=meta,
                            ):
                                db.add(book)
                                updated_books_count += 1
                            isbn_by_platform_id[platform_book_id] = target_isbn
                            continue

                        book = db.get(Book, target_isbn)
                        if not book:
                            values = metadata_book_values(
                                decision,
                                raw_title=raw_title,
                                crawler_cover=crawler_cover,
                                metadata=meta,
                            )
                            book = Book(
                                isbn=target_isbn,
                                title=str(values["title"] or raw_title),
                                author=str(values["author"] or "未知作者"),
                                cover_url=values["cover_url"],
                                category=str(values["category"] or "未分類"),
                            )
                            db.add(book)
                        else:
                            apply_metadata_decision(
                                book,
                                decision,
                                raw_title=raw_title,
                                crawler_cover=crawler_cover,
                                metadata=meta,
                            )
                            db.add(book)

                        new_books_count += 1
                        purchase = Purchase(
                            user_id=user_id,
                            platform="kobo",
                            platform_book_id=platform_book_id,
                            isbn=target_isbn
                        )
                        db.add(purchase)
                        existing_purchases.append(purchase)
                        existing_isbns.add(target_isbn)
                        isbn_by_platform_id[platform_book_id] = target_isbn

                    # 集中單次 commit，避免鎖檔
                    db.commit()
                print(
                    "[Kobo Library Import] 同步完成，"
                    f"新增 {new_books_count} 本，補齊 {updated_books_count} 本"
                )

            await save_platform_storage_state(
                context,
                state_file_path,
                "kobo",
            )
            return {
                "platform": "kobo",
                "status": "success",
                "message": "Kobo 書櫃同步完成",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
                "remote_books": len(remote_books),
            }
        except Exception as e:
            print(f"[Kobo Library Import] 同步過程發生錯誤: {e}")
            return {
                "platform": "kobo",
                "status": "failed",
                "message": str(e),
                "new_books": new_books_count,
                "updated_books": updated_books_count,
            }
        finally:
            await browser.close()
