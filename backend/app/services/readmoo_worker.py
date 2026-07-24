# app/services/readmoo_worker.py
import os
import urllib.parse
from pathlib import Path
from playwright.async_api import Error as PlaywrightError, async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import WishlistItem, Book, PlatformSession
from app.services.platform_auth import (
    get_platform_auth_cookies,
    set_platform_session_status,
)
from app.services.wishlist_reconciliation import (
    remove_stale_synced_wishlist_items,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 透過環境變數控制，預設開啟隱藏模式
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "True").lower() == "true"

def get_user_state_path(user_id: str) -> Path:
    return BASE_DIR / "user_profiles" / user_id / "readmoo" / "state.json"

async def _execute_readmoo_wishlist_action(user_id: str, isbn: str, action: str):
    state_file_path = get_user_state_path(user_id)
    
    if not state_file_path.exists():
        print(f"[Readmoo Worker] 找不到使用者 {user_id} 的憑證檔 {state_file_path}")
        _update_sync_status(user_id, isbn, "auth_expired")
        return

    book_title = None
    with Session(engine) as db:
        book = db.get(Book, isbn)
        if book:
            book_title = book.title

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=IS_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        try:
            print(f"[Readmoo Worker] 開始處理 ISBN: {isbn}，動作: {action}")

            # 1. 前往首頁並檢查憑證是否過期
            await page.goto("https://readmoo.com/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            login_btn_count = await page.locator("a:has-text('登入'), button:has-text('登入')").locator("visible=true").count()
            if login_btn_count > 0:
                print(f"[Readmoo Worker] 偵測到使用者 {user_id} 的憑證已過期！")
                _update_sync_status(user_id, isbn, "auth_expired")
                
                with Session(engine) as db:
                    session_record = db.exec(
                        select(PlatformSession).where(
                            PlatformSession.user_id == user_id,
                            PlatformSession.platform == "readmoo"
                        )
                    ).first()
                    if session_record:
                        session_record.status = "expired"
                        db.commit()
                return

            async def perform_homepage_search(keyword: str) -> str | None:
                print(f"[Readmoo Worker] 前往 Readmoo 首頁準備搜尋...")
                await page.goto("https://readmoo.com/", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)

                search_input = page.locator("input[name='kw']:visible, input[type='search']:visible, input[placeholder*='搜尋']:visible").first
                
                if await search_input.count() == 0:
                    print(f"[Readmoo Worker] 找不到首頁的搜尋輸入框")
                    return None
                    
                print(f"[Readmoo Worker] 於搜尋框鍵入: {keyword}")
                await search_input.click()
                await search_input.fill("")
                await search_input.type(keyword, delay=50)
                
                search_icon = page.locator("i.mo-search").locator("visible=true").first
                if await search_icon.count() > 0:
                    print(f"[Readmoo Worker] 點擊搜尋圖示送出（執行雙擊）")
                    await search_icon.click(force=True)
                    await page.wait_for_timeout(800)
                    await search_icon.click(force=True)
                else:
                    print(f"[Readmoo Worker] 未找到搜尋圖示，使用 Enter 送出")
                    await search_input.press("Enter")

                print(f"[Readmoo Worker] 等待搜尋結果...")
                await page.wait_for_timeout(3000)

                if book_title:
                    short_title = book_title[:4].strip()
                    selector = f"a[title*='{short_title}'], img[title*='{short_title}']"
                else:
                    selector = "a.product-link, img.js-lazy-image"

                try:
                    await page.wait_for_selector(selector, timeout=10000)
                except Exception:
                    print(f"[Readmoo Worker] 搜尋結果載入逾時或無符合項目")
                    return None

                target_element = page.locator(selector).first
                if await target_element.count() > 0:
                    href = await target_element.evaluate("el => el.tagName.toLowerCase() === 'a' ? el.href : el.closest('a').href")
                    if href:
                        return href if href.startswith("http") else f"https://readmoo.com{href}"
                return None

            target_url = await perform_homepage_search(isbn)

            if not target_url and book_title:
                print(f"[Readmoo Worker] ISBN 無結果，切換至純書名搜尋: {book_title}")
                target_url = await perform_homepage_search(book_title.strip())

            if target_url:
                print(f"[Readmoo Worker] 解析到正確書籍 URL，準備進入內頁: {target_url}")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                
                print(f"[Readmoo Worker] 等待內頁待購清單按鈕渲染...")
                try:
                    await page.wait_for_selector("button:has-text('待購清單')", timeout=10000)
                except Exception:
                    pass

                wishlist_btn = page.locator("button:has-text('待購清單')").locator("visible=true").first

                if await wishlist_btn.count() > 0:
                    print(f"[Readmoo Worker] 找到待購清單按鈕，準備判斷是否已在清單中...")
                    await page.wait_for_timeout(2500)

                    btn_html = await wishlist_btn.evaluate("el => el.outerHTML")
                    is_already_in_wishlist = "active" in btn_html or "mo-heart-fill" in btn_html

                    if action == "add":
                        if is_already_in_wishlist:
                            print(f"[Readmoo Worker] 該書籍已經在待購清單中 (愛心已填滿)。")
                            _update_sync_status(user_id, isbn, "synced")
                        else:
                            print(f"[Readmoo Worker] 執行點擊「加入待購清單」...")
                            await wishlist_btn.click(force=True)
                            await page.wait_for_timeout(3000)
                            print(f"[Readmoo Worker] 成功加入待購清單。")
                            _update_sync_status(user_id, isbn, "synced")
                            
                    elif action == "remove":
                        if is_already_in_wishlist:
                            print(f"[Readmoo Worker] 執行再次點擊以「移除待購清單」...")
                            await wishlist_btn.click(force=True)
                            await page.wait_for_timeout(3000)
                            print(f"[Readmoo Worker] 成功移除待購清單。")
                            _update_sync_status(user_id, isbn, "removed")
                        else:
                            print(f"[Readmoo Worker] 該書籍原本就不在待購清單中，無須移除。")
                            _update_sync_status(user_id, isbn, "removed")
                else:
                    print(f"[Readmoo Worker] 進入內頁後找不到待購清單按鈕")
                    _update_sync_status(user_id, isbn, "failed")
            else:
                print(f"[Readmoo Worker] 在 Readmoo 平台上找不到目標書籍")
                _update_sync_status(user_id, isbn, "failed")

        except Exception as e:
            print(f"[Readmoo Worker] 執行過程發生例外錯誤: {e}")
            _update_sync_status(user_id, isbn, "failed")
        finally:
            await browser.close()

def _update_sync_status(user_id: str, isbn: str, status: str):
    try:
        with Session(engine) as db:
            statement = select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.isbn == isbn,
                WishlistItem.platform == "readmoo"
            )
            item = db.exec(statement).first()
            if item:
                item.sync_status = status
                db.commit()
                print(f"[Readmoo Worker] 資料庫狀態已更新為: {status} (ISBN: {isbn})")
    except Exception as e:
        print(f"[Readmoo Worker] 更新資料庫狀態失敗: {e}")

async def add_to_readmoo_wishlist(user_id: str, isbn: str):
    await _execute_readmoo_wishlist_action(user_id, isbn, action="add")

async def remove_from_readmoo_wishlist(user_id: str, isbn: str):
    await _execute_readmoo_wishlist_action(user_id, isbn, action="remove")

async def _readmoo_import_page_status(page) -> str | None:
    """Return a non-success status when the wishlist page is not usable."""
    current_url = page.url.casefold()
    if any(token in current_url for token in ("signin", "login", "oauth2")):
        return "auth_required"

    try:
        body = (await page.locator("body").inner_text()).casefold()
        cookies = await page.context.cookies()
    except PlaywrightError:
        return "parser_error"

    if any(
        marker in body
        for marker in (
            "max challenge attempts exceeded",
            "challenge attempts exceeded",
            "captcha challenge",
        )
    ):
        return "blocked"

    strong_auth_cookie = any(
        cookie.get("name") in {"oauth_token", "oauth_refresh_token"}
        or (
            str(cookie.get("name", "")).startswith(
                "CognitoIdentityServiceProvider."
            )
            and str(cookie.get("name", "")).endswith(
                (".accessToken", ".idToken", ".refreshToken")
            )
        )
        for cookie in get_platform_auth_cookies(cookies, "readmoo")
    )
    return None if strong_auth_cookie else "auth_required"


async def _readmoo_explicitly_empty(page) -> bool:
    empty_selector = (
        ".cart-empty, .empty-cart, .empty-state, "
        "text=/待購清單.*(?:沒有|尚無|目前無)/"
    )
    try:
        return await page.locator(empty_selector).count() > 0
    except PlaywrightError:
        return False


async def import_readmoo_wishlist_to_db(user_id: str) -> dict:
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        print(f"[Readmoo Import] 找不到使用者 {user_id} 的憑證檔，無法同步")
        set_platform_session_status(user_id, "readmoo", "expired")
        return {
            "platform": "readmoo",
            "status": "auth_required",
            "books": 0,
            "message": "找不到 Readmoo 登入憑證，請重新登入",
        }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=IS_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(storage_state=str(state_file_path))
        page = await context.new_page()

        remote_books = []

        try:
            print(f"[Readmoo Import] 開始同步遠端清單...")
            await page.goto("https://readmoo.com/checkout/cart#wishlist", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)

            page_status = await _readmoo_import_page_status(page)
            if page_status == "blocked":
                set_platform_session_status(user_id, "readmoo", "blocked")
                print("[Readmoo Import] 平台安全驗證拒絕存取，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "blocked",
                    "books": 0,
                    "message": "Readmoo 安全驗證拒絕登入，請暫停重試",
                }
            if page_status == "auth_required":
                set_platform_session_status(user_id, "readmoo", "expired")
                print("[Readmoo Import] 偵測到登入頁或失效憑證，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "auth_required",
                    "books": 0,
                    "message": "Readmoo 登入憑證已失效，請重新登入",
                }
            if page_status:
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "無法確認 Readmoo 待購清單頁面狀態",
                }
            
            # 等待清單列表渲染完成
            try:
                await page.wait_for_selector("li.cart-list-item", timeout=10000)
            except PlaywrightError:
                print(f"[Readmoo Import] 等待 cart-list-item 逾時")
            
            await page.wait_for_timeout(2000)

            # 💡 直接以每一個獨立的清單項目 (li.cart-list-item) 作為迴圈單位，避免重複抓取
            book_items = page.locator("li.cart-list-item") 
            count = await book_items.count()
            if count == 0 and not await _readmoo_explicitly_empty(page):
                set_platform_session_status(user_id, "readmoo", "parser_error")
                print("[Readmoo Import] 找不到清單或明確空清單提示，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "Readmoo 頁面沒有可辨識的待購清單資料",
                }
            print(f"[Readmoo Import] 遠端頁面共找到 {count} 個獨立書籍項目")

            for i in range(count):
                item = book_items.nth(i)
                try:
                    # 在每個項目內精準抓取唯一的書名連結
                    title_el = item.locator("a.item-title-link").first
                    title = await title_el.inner_text() if await title_el.count() > 0 else ""
                    href = await title_el.get_attribute("href") if await title_el.count() > 0 else ""
                    
                    isbn = href.split("/")[-1] if href and "book/" in href else "UNKNOWN_ISBN"
                except PlaywrightError as error:
                    print(f"[Readmoo Import] 第 {i + 1} 筆資料解析失敗: {error}")
                    title = ""
                    isbn = "UNKNOWN_ISBN"

                if title and title.strip():
                    remote_books.append({"isbn": str(isbn).strip(), "title": title.strip()})

            print(f"[Readmoo Import] 確認同步的書籍數: {len(remote_books)}")

            with Session(engine) as db:
                for b_info in remote_books:
                    isbn = b_info["isbn"]
                    title = b_info["title"]

                    book = db.get(Book, isbn)
                    if not book:
                        book = Book(isbn=isbn, title=title)
                        db.add(book)
                        db.commit()

                    statement = select(WishlistItem).where(
                        WishlistItem.user_id == user_id,
                        WishlistItem.isbn == isbn,
                        WishlistItem.platform == "readmoo"
                    )
                    wish_item = db.exec(statement).first()

                    if not wish_item:
                        new_wish_item = WishlistItem(
                            user_id=user_id,
                            isbn=isbn,
                            platform="readmoo",
                            sync_status="synced"
                        )
                        db.add(new_wish_item)
                    else:
                        if wish_item.sync_status != "synced":
                            wish_item.sync_status = "synced"
                            db.add(wish_item)

                removed_count = remove_stale_synced_wishlist_items(
                    db,
                    user_id,
                    "readmoo",
                    remote_books,
                )
                db.commit()
            set_platform_session_status(user_id, "readmoo", "active")
            print(
                "[Readmoo Import] 資料庫同步完成！"
                f" 已移除 {removed_count} 筆遠端不存在的同步項目"
            )
            return {
                "platform": "readmoo",
                "status": "success",
                "books": len(remote_books),
                "removed": removed_count,
                "message": f"Readmoo 待購清單同步完成（{len(remote_books)} 本）",
            }

        except Exception as e:
            print(f"[Readmoo Import] 同步過程發生錯誤: {e}")
            set_platform_session_status(user_id, "readmoo", "parser_error")
            return {
                "platform": "readmoo",
                "status": "parser_error",
                "books": 0,
                "message": "Readmoo 待購清單同步失敗",
            }
        finally:
            await browser.close()
