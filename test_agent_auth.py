import sys
import logging
from playwright.sync_api import sync_playwright
from agent import authenticate_browser_context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_agent_auth")

def run():
    url = "https://sellingo.ai"
    auth = {
        "auth_required": True,
        "auth_login_url": "",
        "auth_post_login_url": "",
        "auth_username": "8983587710",
        "auth_password": "999999",
        "auth_otp_code": "",
    }
    
    logger.info("Starting agent auth test with re-navigation...")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        
        # Call the actual agent helper!
        success = authenticate_browser_context(page, auth, url, logger.info)
        logger.info(f"Authentication success returned: {success}")
        
        logger.info(f"URL immediately after login: {page.url}")
        
        # Re-navigate to homepage!
        logger.info("Navigating back to https://sellingo.ai explicitly...")
        page.goto("https://sellingo.ai", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        
        logger.info(f"URL after re-navigation: {page.url}")
        logger.info(f"Title after re-navigation: {page.title()}")
        page.screenshot(path="agent_auth_re_nav.png")
        
        body_text = page.locator("body").inner_text()
        logger.info(f"Body text success markers check: logout={('logout' in body_text.lower())}, profile={('profile' in body_text.lower())}, dashboard={('dashboard' in body_text.lower())}")
        
        links = page.locator("a").all()
        logger.info(f"Total links after re-navigation: {len(links)}")
        for l in links[:15]:
            try:
                href = l.get_attribute("href")
                text_val = l.inner_text().strip()
                if href or text_val:
                    logger.info(f"  Link: href={href}, text='{text_val}'")
            except Exception:
                pass
                
        browser.close()

if __name__ == "__main__":
    run()
