import asyncio
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # 1. Desktop Test
        print("=== RUNNING DESKTOP TEST ===")
        desktop_context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        desktop_page = desktop_context.new_page()
        
        desktop_page.on("console", lambda msg: print(f"[Desktop Console] {msg.type}: {msg.text}"))
        desktop_page.on("pageerror", lambda err: print(f"[Desktop Error] {err}"))
        
        try:
            desktop_page.goto("http://192.168.10.125:3000/", wait_until="networkidle", timeout=10000)
            print("Desktop Page Title:", desktop_page.title())
            print("Desktop Page HTML length:", len(desktop_page.content()))
            desktop_page.screenshot(path="screenshots/diagnose_desktop.png")
            print("Saved screenshots/diagnose_desktop.png")
        except Exception as e:
            print(f"Desktop load failed: {e}")
            try:
                desktop_page.screenshot(path="screenshots/diagnose_desktop_error.png")
            except Exception:
                pass
        
        desktop_context.close()
        
        # 2. Mobile Test
        print("\n=== RUNNING MOBILE TEST ===")
        iphone = p.devices['iPhone 12']
        mobile_context = browser.new_context(
            **iphone,
            ignore_https_errors=True
        )
        mobile_page = mobile_context.new_page()
        
        mobile_page.on("console", lambda msg: print(f"[Mobile Console] {msg.type}: {msg.text}"))
        mobile_page.on("pageerror", lambda err: print(f"[Mobile Error] {err}"))
        
        try:
            mobile_page.goto("http://192.168.10.125:3000/", wait_until="networkidle", timeout=10000)
            print("Mobile Page Title:", mobile_page.title())
            print("Mobile Page HTML length:", len(mobile_page.content()))
            mobile_page.screenshot(path="screenshots/diagnose_mobile.png")
            print("Saved screenshots/diagnose_mobile.png")
        except Exception as e:
            print(f"Mobile load failed: {e}")
            try:
                mobile_page.screenshot(path="screenshots/diagnose_mobile_error.png")
            except Exception:
                pass
                
        mobile_context.close()
        browser.close()

if __name__ == "__main__":
    run()
