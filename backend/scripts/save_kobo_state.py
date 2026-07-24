import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE_PATH = BASE_DIR / "kobo_state.json"

async def save_kobo_state():
    async with async_playwright() as p:
        # 1. 啟動 Chrome/Chromium 並加入避開檢測的參數 (chromium-sandbox / automation args)
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled", # 隱藏自動化控制特徵
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
        )

        # 2. 模擬真實的桌面版 Chrome User-Agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )

        page = await context.new_page()

        # 3. 透過 WebDriver 屬性覆蓋，隱藏 navigator.webdriver 標誌
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        print("正在開啟 Kobo 台灣官網...")
        await page.goto("https://www.kobo.com/tw/zh", wait_until="domcontentloaded")
        
        print("\n==================================================")
        print("請在開啟的 Chrome 視窗中：")
        print("1. 點擊右上角的「登入」。")
        print("2. 進行帳號密碼登入。")
        print("==================================================\n")
        
        input("登入成功並確認看到個人帳號後，請在此按 [Enter] 鍵繼續...")

        # 4. 匯出並儲存狀態
        await context.storage_state(path=str(STATE_FILE_PATH))
        print(f"\n✅ 成功將 Kobo 登入狀態儲存至: {STATE_FILE_PATH}")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(save_kobo_state())