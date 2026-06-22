import sys
from playwright.sync_api import sync_playwright

def run():
    url = "https://sellingo.ai"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        
        # Click login trigger
        page.locator("a:has-text('LOGIN'), button:has-text('LOGIN')").first.click()
        page.wait_for_timeout(2000)
        
        # Get outer HTML of all buttons/inputs/anchors in the modal containing 'login'
        print("=== MODAL HTML SNIPPET ===")
        # The modal container is likely div.modal or div with id/class containing login
        # Let's locate elements by text 'LOGIN'
        login_el = page.locator("text=LOGIN").all()
        for idx, el in enumerate(login_el):
            try:
                tag = el.evaluate("el => el.tagName")
                outer_html = el.evaluate("el => el.outerHTML")
                print(f"Element {idx}: tag={tag}, text='{el.inner_text()}'")
                print(outer_html[:500])
                print("-" * 40)
            except Exception as e:
                print(f"Error evaluating element {idx}: {e}")
                
        # Find all inputs in the modal specifically
        print("\n=== MODAL INPUTS ===")
        inputs = page.locator("input").all()
        for idx, inp in enumerate(inputs):
            try:
                visible = inp.is_visible()
                if visible:
                    print(f"Input {idx}: name={inp.get_attribute('name')}, id={inp.get_attribute('id')}, placeholder={inp.get_attribute('placeholder')}")
            except Exception:
                pass
                
        browser.close()

if __name__ == "__main__":
    run()
