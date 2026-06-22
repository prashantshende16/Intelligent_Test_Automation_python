import sys
import logging
import json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from agent import normalize_url, same_site, extract_page_snapshot, harvest_navigation_links, click_navigation_items_for_routes, authenticate_browser_context, expand_navigation_regions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_crawl_step")

def run():
    url = "https://sellingo.ai"
    auth = {
        "auth_required": True,
        "auth_login_url": "",
        "auth_post_login_url": "https://sellingo.ai/merchant/mycatalog",
        "auth_username": "8983587710",
        "auth_password": "999999",
        "auth_otp_code": "",
    }
    
    normalized = normalize_url(url)
    snapshots = []
    visited = set()
    queue = [normalized]
    
    logger.info("Initializing Playwright...")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        
        logger.info("Running authenticate_browser_context...")
        auth_success = authenticate_browser_context(page, auth, normalized, logger.info)
        logger.info(f"Auth success: {auth_success}")
        
        try:
            expand_navigation_regions(page)
        except Exception as e:
            logger.info(f"expand_navigation_regions error: {e}")
            
        logger.info(f"Initial queue: {queue}")
        iteration = 0
        while queue and len(snapshots) < 10:
            iteration += 1
            current_url = queue.pop(0)
            logger.info(f"[Iter {iteration}] Popped URL: {current_url}")
            
            if current_url.rstrip("/") in visited:
                logger.info(f"[Iter {iteration}] URL already visited. Skipping.")
                continue
            visited.add(current_url.rstrip("/"))
            
            response = None
            try:
                logger.info(f"[Iter {iteration}] Navigating to {current_url}...")
                response = page.goto(current_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception as e:
                    logger.info(f"[Iter {iteration}] wait_for_load_state warning: {e}")
                try:
                    expand_navigation_regions(page)
                except Exception:
                    pass
            except PlaywrightTimeoutError as exc:
                logger.warning(f"[Iter {iteration}] Timeout loading {current_url}: {exc}")
            except Exception as exc:
                logger.warning(f"[Iter {iteration}] Failed to navigate to {current_url}: {exc}")
                
            actual_url = page.url
            logger.info(f"[Iter {iteration}] actual_url = {actual_url}")
            
            if actual_url != current_url and same_site(actual_url, normalized):
                if actual_url.rstrip("/") in visited:
                    logger.info(f"[Iter {iteration}] actual_url already visited. Skipping loop rest.")
                    continue
                visited.add(actual_url.rstrip("/"))
                current_url = actual_url
                
            status_code = response.status if response else 500
            logger.info(f"[Iter {iteration}] status_code = {status_code}")
            
            try:
                logger.info(f"[Iter {iteration}] Extracting snapshot...")
                snapshot = extract_page_snapshot(page, current_url, status_code)
                snapshots.append(snapshot)
                logger.info(f"[Iter {iteration}] Snapshot added. Total snapshots: {len(snapshots)}")
            except Exception as exc:
                logger.error(f"[Iter {iteration}] Failed to extract snapshot: {exc}")
                continue
                
            link_hrefs = harvest_navigation_links(page, current_url)
            clicked_hrefs = click_navigation_items_for_routes(page, current_url)
            logger.info(f"[Iter {iteration}] Harvested {len(link_hrefs)} links and {len(clicked_hrefs)} clicked hrefs.")
            
            high_priority_candidates = []
            normal_priority_candidates = []
            for href in link_hrefs + clicked_hrefs:
                candidate_href = href.get("href") if isinstance(href, dict) else href
                if candidate_href and same_site(candidate_href, normalized) and candidate_href.rstrip("/") not in visited and candidate_href not in queue:
                    is_dynamic = any(d in candidate_href.lower() for d in ["/edit", "/delete", "/update", "/show", "/view", "/detail", "?", "#"])
                    is_auth_kw = any(nb in candidate_href.lower() for nb in ["logout", "signout", "login", "signin"])
                    is_admin_dashboard = any(p in candidate_href.lower() for p in ["/admin", "/dashboard", "/app", "/portal"])
                    
                    if is_admin_dashboard and not is_dynamic and not is_auth_kw:
                        if candidate_href not in high_priority_candidates:
                            high_priority_candidates.append(candidate_href)
                    else:
                        if candidate_href not in normal_priority_candidates:
                            normal_priority_candidates.append(candidate_href)
            
            queue = high_priority_candidates + queue
            queue.extend(normal_priority_candidates)
            logger.info(f"[Iter {iteration}] New queue length: {len(queue)}, sample: {queue[:5]}")
            
        browser.close()
        logger.info(f"Completed step crawl. Discovered snapshots: {len(snapshots)}")

if __name__ == "__main__":
    run()
