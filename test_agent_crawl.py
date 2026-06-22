import sys
import logging
from playwright.sync_api import sync_playwright
from agent import discover_pages_with_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_agent_crawl")

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
    
    logger.info("Starting agent crawl test with post-login URL...")
    snapshots = discover_pages_with_playwright(url, auth=auth, max_pages=10)
    
    logger.info(f"Crawl completed. Discovered {len(snapshots)} snapshots:")
    for idx, snap in enumerate(snapshots):
        logger.info(f"Snapshot {idx+1}: URL={snap.get('page_url')}, Title={snap.get('title')}, LinksCount={len(snap.get('links', []))}")
        # Print a few links
        links = snap.get('links', [])
        logger.info("  Sample links:")
        for l in links[:3]:
            logger.info(f"    href={l.get('href')}, text='{l.get('text')}'")

if __name__ == "__main__":
    run()
