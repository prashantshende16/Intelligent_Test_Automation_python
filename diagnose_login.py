import sys
import os
import json
import logging
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("diagnose_login")

def run():
    url = "https://sellingo.ai"
    username = "8983587710"
    password = "999999"
    
    logger.info("Starting Playwright diagnostic...")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        
        logger.info(f"Navigating to {url}...")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        
        logger.info(f"Page title: {page.title()}")
        logger.info(f"Current URL: {page.url}")
        
        # Take a screenshot before attempting login
        page.screenshot(path="before_login.png")
        logger.info("Screenshot saved as before_login.png")
        
        # Check login triggers
        # Let's inspect the page inputs
        inputs = page.locator("input").all()
        logger.info(f"Found {len(inputs)} inputs on load:")
        for idx, inp in enumerate(inputs):
            try:
                logger.info(f"  Input {idx}: type={inp.get_attribute('type')}, name={inp.get_attribute('name')}, id={inp.get_attribute('id')}, placeholder={inp.get_attribute('placeholder')}, visible={inp.is_visible()}")
            except Exception as e:
                logger.info(f"  Input {idx} error: {e}")
                
        # Find trigger buttons/links
        logger.info("Looking for Login trigger...")
        login_btn = page.locator("a:has-text('Login'), button:has-text('Login'), a:has-text('LOGIN'), button:has-text('LOGIN')")
        logger.info(f"Found {login_btn.count()} matching elements for 'Login'")
        
        if login_btn.count() > 0:
            for idx in range(login_btn.count()):
                btn = login_btn.nth(idx)
                logger.info(f"  Trigger {idx}: text='{btn.inner_text()}', visible={btn.is_visible()}")
                if btn.is_visible():
                    logger.info(f"Clicking trigger {idx}...")
                    btn.click()
                    page.wait_for_timeout(3000)
                    break
        else:
            logger.info("No login trigger found by text. Let's try custom locator '.login_popup_register'")
            popup = page.locator(".login_popup_register")
            if popup.count() > 0:
                logger.info("Clicking .login_popup_register...")
                popup.first.click()
                page.wait_for_timeout(3000)
                
        page.screenshot(path="after_trigger_click.png")
        logger.info("Screenshot after click saved as after_trigger_click.png")
        
        # Check inputs again
        inputs = page.locator("input").all()
        logger.info(f"Found {len(inputs)} inputs after trigger:")
        for idx, inp in enumerate(inputs):
            try:
                logger.info(f"  Input {idx}: type={inp.get_attribute('type')}, name={inp.get_attribute('name')}, id={inp.get_attribute('id')}, placeholder={inp.get_attribute('placeholder')}, visible={inp.is_visible()}")
            except Exception as e:
                logger.info(f"  Input {idx} error: {e}")
                
        # Let's locate the mobile / email input and password input
        # Typical selectors: input[type='text'], input[type='password'] or input[name*='mobile'] etc.
        mobile_input = None
        password_input = None
        
        # Find inputs again
        for inp in inputs:
            try:
                if not inp.is_visible():
                    continue
                name = (inp.get_attribute("name") or "").lower()
                id_val = (inp.get_attribute("id") or "").lower()
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                typ = (inp.get_attribute("type") or "").lower()
                
                if "mobile" in name or "mobile" in id_val or "mobile" in placeholder or "phone" in name or "phone" in id_val or "phone" in placeholder:
                    mobile_input = inp
                elif typ == "password" or "pass" in name or "pass" in id_val or "pass" in placeholder:
                    password_input = inp
            except Exception:
                pass
                
        if not mobile_input:
            # Fallback to first visible text input
            for inp in inputs:
                try:
                    if inp.is_visible() and (inp.get_attribute("type") == "text" or not inp.get_attribute("type")):
                        mobile_input = inp
                        break
                except Exception:
                    pass
                    
        if not password_input:
            for inp in inputs:
                try:
                    if inp.is_visible() and inp.get_attribute("type") == "password":
                        password_input = inp
                        break
                except Exception:
                    pass
                    
        if mobile_input:
            logger.info("Filling mobile input...")
            mobile_input.fill(username)
        else:
            logger.warning("Could not identify mobile input!")
            
        if password_input:
            logger.info("Filling password input...")
            password_input.fill(password)
        else:
            logger.warning("Could not identify password input!")
            
        page.screenshot(path="inputs_filled.png")
        logger.info("Screenshot of filled inputs saved as inputs_filled.png")
        
        # Click submit button
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "button:has-text('Submit')",
            "button:has-text('Verify')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('Send')",
            "text=Login",
            "text=Sign in",
        ]
        
        submitted = False
        for sel in submit_selectors:
            loc = page.locator(sel)
            for j in range(loc.count()):
                btn = loc.nth(j)
                if btn.is_visible():
                    logger.info(f"Clicking submit button: {sel}")
                    btn.click()
                    submitted = True
                    break
            if submitted:
                break
                
        if not submitted:
            logger.warning("Could not find a visible submit button!")
            
        page.wait_for_timeout(5000)
        logger.info(f"URL after submission: {page.url}")
        logger.info(f"Page title after submission: {page.title()}")
        
        page.screenshot(path="after_submission.png")
        logger.info("Screenshot after submission saved as after_submission.png")
        
        # Check if there are dashboard/admin links or logout link now
        body_text = page.locator("body").inner_text()
        logger.info(f"Success indicators: logout={('logout' in body_text.lower())}, profile={('profile' in body_text.lower())}, dashboard={('dashboard' in body_text.lower())}")
        
        # Get all links on page after login
        links = page.locator("a").all()
        logger.info(f"Total links after login: {len(links)}")
        internal_links = []
        for l in links:
            try:
                href = l.get_attribute("href")
                text_val = l.inner_text().strip()
                if href:
                    logger.info(f"  Link: href={href}, text='{text_val}'")
                    if href.startswith("/") or "sellingo.ai" in href:
                        internal_links.append(href)
            except Exception:
                pass
                
        context.close()
        browser.close()
        logger.info("Diagnostic completed successfully.")

if __name__ == "__main__":
    run()
