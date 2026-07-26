import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from app.services.platform_auth import launch_readmoo_browser

BASE_DIR = Path(__file__).resolve().parent.parent
# 為了對應我們多使用者的路徑，您可以直接存到對應使用者的資料夾中
# 例如: backend/user_profiles/test_user_001/readmoo/state.json
STATE_FILE_PATH = BASE_DIR / "user_profiles" / "test_user_001" / "readmoo" / "state.json"

async def save_readmoo_state():
    STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    async with async_playwright() as p:
        # 開啟瀏覽器，加上防偵測參數
        browser = await launch_readmoo_browser(p, headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print("正在開啟 Readmoo 首頁...")
        
        # 直接去 Readmoo 主站首頁（最安全、不易被 Cloudflare 攔截）
        await page.goto("https://readmoo.com", wait_until="domcontentloaded")

        print("\n==================================================")
        print("請在彈出的 Chrome 視窗中：")
        print("1. 自行點擊右上角『登入』")
        print("2. 完成帳號密碼與驗證碼登入")
        print("3. 確認畫面已經成功登入（看到會員帳號/頭像）")
        print("==================================================\n")
        
        input("完成上述登入步驟後，請回到此終端機按 [Enter] 鍵儲存憑證...")

        # 匯出並儲存 state
        await context.storage_state(path=str(STATE_FILE_PATH))
        print(f"\n✅ 成功將最新登入狀態儲存至: {STATE_FILE_PATH}")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(save_readmoo_state())
