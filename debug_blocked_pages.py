import sqlite3
import json
import sys
import os

# Set up paths so it can import local modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SessionLocal
from models import Task, TestCase, TestError

def looks_like_blocked_page_debug(title: str = "", html_snippet: str = "", body_text: str = "") -> dict:
    BLOCKED_PAGE_MARKERS = (
        "just a moment",
        "access denied",
        "attention required",
        "cloudflare",
        "checking your browser",
    )
    haystack = " ".join([title or "", html_snippet or "", body_text or ""]).lower()
    reasons = []
    
    if not haystack.strip():
        return {"blocked": True, "reasons": ["empty haystack"]}

    for marker in BLOCKED_PAGE_MARKERS:
        if marker in haystack:
            reasons.append(f"matched marker: {marker}")

    javascript_warning = "enable javascript" in haystack or "javascript is required" in haystack
    challenge_terms = (
        "checking your browser",
        "verify you are human",
        "security check",
        "ddos protection",
        "ray id",
        "cf-browser-verification",
        "cf-challenge",
        "captcha",
    )
    normal_app_terms = (
        "sign in",
        "login",
        "password",
        "forgot",
        "dashboard",
        "logout",
        "profile",
    )
    
    if javascript_warning and any(term in haystack for term in challenge_terms):
        matched_terms = [t for t in challenge_terms if t in haystack]
        reasons.append(f"js_warning AND matched challenge_terms: {matched_terms}")
        
    if javascript_warning and not any(term in haystack for term in normal_app_terms) and len(haystack) < 500:
        reasons.append("js_warning AND no normal_app_terms AND len < 500")

    return {"blocked": len(reasons) > 0, "reasons": reasons}

def main():
    db = SessionLocal()
    # Get the latest task
    task = db.query(Task).order_by(Task.created_at.desc()).first()
    if not task:
        print("No task found in database.")
        return
    
    print(f"Analyzing Task ID: {task.id} (URL: {task.url})")
    
    if not task.page_snapshots_json:
        print("No page snapshots found in task.")
        return
        
    pages = json.loads(task.page_snapshots_json)
    print(f"Total page snapshots: {len(pages)}")
    
    for idx, page in enumerate(pages):
        url = page.get("page_url")
        title = page.get("title") or "Untitled"
        html = page.get("html_snippet") or ""
        body = page.get("body_text") or ""
        res = looks_like_blocked_page_debug(title, html, body)
        
        if res["blocked"]:
            print(f"\nPage {idx}: URL={url}")
            print(f"  Title: {title}")
            print(f"  HTML Length: {len(html)}")
            print(f"  Reasons: {res['reasons']}")
            # Check if any normal app terms exist on the page anyway
            haystack_lower = " ".join([title, html, body]).lower()
            normal_present = [t for t in ["sign in", "login", "password", "forgot", "dashboard", "logout", "profile"] if t in haystack_lower]
            print(f"  Normal terms present anyway: {normal_present}")
            
    db.close()

if __name__ == "__main__":
    main()
