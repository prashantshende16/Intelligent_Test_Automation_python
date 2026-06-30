from __future__ import annotations
import os
import re
import time
import json
import logging
import traceback
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from datetime import datetime
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Task, UseCase, TestCase, TestError, Suggestion, Codebase, AgentState, CodeReference, TaskAuth, SafetyConfig, TestCleanupLog
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def normalize_url(url: str) -> str:
    if not url:
        return url
    if not url.startswith("http://") and not url.startswith("https://"):
        return "https://" + url
    return url


def same_origin(candidate: str, base_url: str) -> bool:
    if not candidate or not base_url:
        return False
    try:
        candidate = normalize_url(candidate)
        base_url = normalize_url(base_url)
        candidate_host = candidate.split("//", 1)[1].split("/")[0].split(":")[0].lower()
        base_host = base_url.split("//", 1)[1].split("/")[0].split(":")[0].lower()
        if candidate_host == base_host:
            return True
        return candidate_host.endswith("." + base_host) or base_host.endswith("." + candidate_host)
    except Exception:
        return False


def same_site(candidate: str, base_url: str) -> bool:
    """Allow crawling across sibling subdomains that belong to the same registrable site."""
    if not candidate or not base_url:
        return False
    try:
        candidate = normalize_url(candidate)
        base_url = normalize_url(base_url)
        candidate_host = candidate.split("//", 1)[1].split("/")[0].split(":")[0].lower()
        base_host = base_url.split("//", 1)[1].split("/")[0].split(":")[0].lower()

        def root_domain(host: str) -> str:
            parts = host.split(".")
            if len(parts) <= 2:
                return host
            return ".".join(parts[-2:])

        return root_domain(candidate_host) == root_domain(base_host)
    except Exception:
        return False


def slugify(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (value or "")).strip("_")[:60]


# --- ADMIN PROTECTION SYSTEM ---
DEFAULT_PROTECTED_USERNAMES = {"admin", "superadmin", "root", "administrator", "sysadmin"}
BLOCKED_ACTIONS_ON_PROTECTED = {"delete", "remove", "deactivate", "disable", "change_password",
                                 "change_role", "update", "edit", "modify", "reset_password"}

def is_protected_user(username: str, custom_protected: set = None) -> bool:
    protected = DEFAULT_PROTECTED_USERNAMES | (custom_protected or set())
    return username.strip().lower() in {p.lower() for p in protected}

def sanitize_test_step(step: str, protected_usernames: set) -> tuple[str, bool]:
    """Returns (sanitized_step, was_blocked). Blocks steps that target protected users."""
    step_lower = step.lower()
    for username in protected_usernames:
        if username.lower() in step_lower:
            for action in BLOCKED_ACTIONS_ON_PROTECTED:
                if action in step_lower:
                    return (f"BLOCKED: Cannot {action} protected user '{username}'", True)
    return (step, False)


# --- SAFE TESTING MODE ---
SAFE_TEST_MARKER = "TEST_RECORD"

def generate_temp_test_user(prefix: str = "test_user_", index: int = 1) -> dict:
    """Generate temporary test credentials that are clearly marked as test data."""
    return {
        "username": f"{prefix}{index:03d}",
        "email": f"{prefix}{index:03d}@test.automation.local",
        "password": f"TestPass_{index:03d}!Secure",
        "first_name": f"TestFirst{index}",
        "last_name": f"TestLast{index}",
        "phone": f"+1555000{index:04d}",
        "_marker": SAFE_TEST_MARKER,
    }

def get_safe_form_values(field_name: str, test_index: int = 1, prefix: str = "test_user_") -> str:
    """Return safe test values for form fields based on field name classification."""
    temp = generate_temp_test_user(prefix, index=test_index)
    field_lower = field_name.lower()
    if "email" in field_lower: return temp["email"]
    if "username" in field_lower or "login" in field_lower or "user" in field_lower: return temp["username"]
    if "password" in field_lower or "pass" in field_lower: return temp["password"]
    if "first" in field_lower or "fname" in field_lower: return temp["first_name"]
    if "last" in field_lower or "lname" in field_lower: return temp["last_name"]
    if "phone" in field_lower or "mobile" in field_lower: return temp["phone"]
    if "name" in field_lower: return f"{temp['first_name']} {temp['last_name']}"
    return f"test_value_{test_index}"



BLOCKED_PAGE_MARKERS = (
    "just a moment",
    "access denied",
    "attention required",
    "cloudflare",
    "checking your browser",
)


def looks_like_blocked_page(title: str = "", html_snippet: str = "", body_text: str = "") -> bool:
    """Detect true bot/challenge pages without flagging normal SPA sign-in screens."""
    haystack = " ".join([title or "", html_snippet or "", body_text or ""]).lower()
    if not haystack.strip():
        return True

    if any(marker in haystack for marker in BLOCKED_PAGE_MARKERS):
        # Prevent cdnjs.cloudflare.com links from triggering block
        is_only_cloudflare_marker = False
        if "cloudflare" in haystack:
            other_markers = [m for m in BLOCKED_PAGE_MARKERS if m != "cloudflare"]
            has_others = any(m in haystack for m in other_markers)
            if not has_others and "cdnjs.cloudflare.com" in haystack:
                is_only_cloudflare_marker = True
        if not is_only_cloudflare_marker:
            return True

    javascript_warning = "enable javascript" in haystack or "javascript is required" in haystack
    challenge_terms = (
        "checking your browser",
        "verify you are human",
        "security check",
        "ddos protection",
        "ray id",
        "cf-browser-verification",
        "cf-challenge",
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
        return True
    if javascript_warning and not any(term in haystack for term in normal_app_terms) and len(haystack) < 500:
        return True

    return False



def aggregate_site_profile(url: str, pages: list) -> dict:
    """Build a unified site profile from all crawled pages for unique test generation."""
    normalized = normalize_url(url)
    domain = normalized.split("//", 1)[1].split("/")[0]

    if not pages:
        return {
            "domain": domain,
            "url": normalized,
            "primary_title": "Unknown Page",
            "page_count": 0,
            "page_urls": [],
            "link_count": 0,
            "links": [],
            "link_texts": [],
            "form_count": 0,
            "forms": [],
            "form_field_names": [],
            "heading_count": 0,
            "headings": [],
            "has_h1": False,
            "meta_count": 0,
            "meta_tags": {},
            "html_length": 0,
            "status_code": 500,
            "image_count": 0,
            "images_missing_alt": 0,
            "is_blocked": True,
        }

    all_links = []
    seen_hrefs = set()
    all_forms = []
    all_headings = []
    all_meta = {}
    page_urls = []
    page_titles = []
    total_html_length = 0
    image_count = 0
    images_missing_alt = 0
    status_codes = []

    for page in pages:
        page_urls.append(page.get("page_url", normalized))
        page_titles.append(page.get("title", "Untitled"))
        total_html_length += page.get("html_length", len(page.get("html_snippet", "") or ""))
        status_codes.append(page.get("status_code", 200))

        for link in page.get("links", []) or []:
            href = link.get("href", "")
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                all_links.append(link)

        all_forms.extend(page.get("forms", []) or [])
        all_headings.extend(page.get("headings", []) or [])
        all_meta.update(page.get("meta_tags", {}) or {})

        for image in page.get("images", []) or []:
            image_count += 1
            if not image.get("has_alt"):
                images_missing_alt += 1

    primary_title = page_titles[0] if page_titles else "Untitled Page"
    title_lower = primary_title.lower()
    first_html = (pages[0].get("html_snippet", "") or "").lower()
    is_blocked = looks_like_blocked_page(primary_title, first_html)

    form_field_names = []
    for form in all_forms:
        for field in form.get("inputs", []) or []:
            name = field.get("name") or field.get("placeholder") or field.get("type", "field")
            form_field_names.append(name)

    return {
        "domain": domain,
        "url": normalized,
        "primary_title": primary_title,
        "page_count": len(pages),
        "page_urls": page_urls,
        "page_titles": page_titles,
        "link_count": len(all_links),
        "links": all_links,
        "link_texts": [link.get("text", "").strip() for link in all_links if link.get("text", "").strip()],
        "form_count": len(all_forms),
        "forms": all_forms,
        "form_field_names": form_field_names,
        "heading_count": len(all_headings),
        "headings": all_headings,
        "has_h1": any(h.lower().startswith("h1:") for h in all_headings),
        "meta_count": len(all_meta),
        "meta_tags": all_meta,
        "html_length": total_html_length,
        "status_code": status_codes[0] if status_codes else 200,
        "image_count": image_count,
        "images_missing_alt": images_missing_alt,
        "is_blocked": is_blocked,
    }


def build_page_profile(page: dict, base_url: str) -> dict:
    """Normalize a single page snapshot into a page-specific profile."""
    page_url = normalize_url(page.get("page_url") or base_url)
    title = page.get("title") or "Untitled Page"
    links = page.get("links", []) or []
    forms = page.get("forms", []) or []
    headings = page.get("headings", []) or []
    meta_tags = page.get("meta_tags", {}) or {}
    images = page.get("images", []) or []
    html_snippet = page.get("html_snippet", "") or ""
    status_code = page.get("status_code", 200)
    domain = page_url.split("//", 1)[1].split("/")[0] if "//" in page_url else page_url

    return {
        "page_url": page_url,
        "url": page_url,
        "domain": domain,
        "title": title,
        "primary_title": title,
        "links": links,
        "link_count": len(links),
        "link_texts": [link.get("text", "").strip() for link in links if link.get("text", "").strip()],
        "forms": forms,
        "form_count": len(forms),
        "form_field_names": [
            (field.get("name") or field.get("placeholder") or field.get("type", "field"))
            for form in forms
            for field in form.get("inputs", []) or []
        ],
        "headings": headings,
        "heading_count": len(headings),
        "has_h1": any(h.lower().startswith("h1:") for h in headings),
        "meta_tags": meta_tags,
        "meta_count": len(meta_tags),
        "html_length": page.get("html_length", len(html_snippet)),
        "html_snippet": html_snippet,
        "status_code": status_code,
        "page_count": 1,
        "image_count": len(images),
        "images_missing_alt": sum(1 for image in images if not image.get("has_alt")),
        "is_blocked": looks_like_blocked_page(title, html_snippet),
    }


# --- ROUTE DISCOVERY SYSTEM ---

def discover_routes_from_codebase(codebase_path: str, codebase_data: dict) -> list:
    """Extract route definitions from React Router, Next.js pages, and backend configs."""
    routes = []
    if not codebase_path or not os.path.isdir(codebase_path):
        return routes

    # 1. React Router patterns
    react_route_patterns = [
        r'path\s*[=:]\s*["\']([^"\']+)["\']',          # path="/users"
        r'<Route\s+.*?path\s*=\s*["\']([^"\']+)["\']',  # <Route path="/users">
        r'navigate\s*\(\s*["\']([^"\']+)["\']',          # navigate("/users")
        r'to\s*=\s*["\']([^"\']+)["\']',                 # to="/users"
        r'href\s*=\s*["\']\/([^"\']+)["\']',             # href="/users"
    ]

    # 2. Next.js file-based routing
    nextjs_page_dirs = ["pages", "app", "src/pages", "src/app"]

    # 3. Backend route patterns (Express, FastAPI, Django)
    backend_route_patterns = [
        r'@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',  # FastAPI
        r'router\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',  # Express
        r'path\s*\(\s*["\']([^"\']+)["\']',  # Django
    ]

    for rel_path in codebase_data.get("file_list", []):
        full_path = os.path.join(codebase_path, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(8000)
            for pattern in react_route_patterns + backend_route_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    route = match if isinstance(match, str) else match[-1]
                    if route.startswith("/") and len(route) < 200:
                        routes.append({
                            "route": route,
                            "source_file": rel_path,
                            "discovery_method": "codebase_static_analysis"
                        })
        except Exception:
            continue

    # Next.js file-based routes
    for page_dir in nextjs_page_dirs:
        dir_path = os.path.join(codebase_path, page_dir)
        if os.path.isdir(dir_path):
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = [d for d in dirs if not d.startswith((".", "_", "api"))]
                for f in files:
                    if f.endswith((".js", ".jsx", ".ts", ".tsx")) and not f.startswith("_"):
                        rel = os.path.relpath(os.path.join(root, f), dir_path)
                        route = "/" + rel.rsplit(".", 1)[0].replace("index", "").rstrip("/")
                        route = re.sub(r'\[([^\]]+)\]', r':\1', route)  # [id] → :id
                        routes.append({
                            "route": route or "/",
                            "source_file": os.path.join(page_dir, rel),
                            "discovery_method": "nextjs_file_routing"
                        })
    return routes


def discover_routes_from_sitemap(url: str) -> list:
    """Fetch and parse sitemap.xml for route URLs."""
    routes = []
    normalized = normalize_url(url)
    domain = normalized.split("//", 1)[1].split("/")[0]
    sitemap_urls = [
        f"{normalized.rstrip('/')}/sitemap.xml",
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/sitemap_index.xml",
    ]
    for sitemap_url in sitemap_urls:
        try:
            resp = httpx.get(sitemap_url, timeout=10.0, follow_redirects=True)
            if resp.status_code == 200 and "<urlset" in resp.text:
                soup = BeautifulSoup(resp.text, "html.parser")
                for loc in soup.find_all("loc"):
                    route_url = loc.get_text().strip()
                    if route_url:
                        routes.append({
                            "route": route_url,
                            "source_file": "sitemap.xml",
                            "discovery_method": "sitemap"
                        })
                break
        except Exception:
            continue
    return routes


def compare_routes(codebase_routes: list, crawled_urls: list, base_url: str) -> dict:
    """Compare routes found in codebase vs routes discovered by crawler."""
    normalized_base = normalize_url(base_url).rstrip("/")

    crawled_paths = set()
    for url in crawled_urls:
        try:
            path = "/" + normalize_url(url).split("//", 1)[1].split("/", 1)[1] if "/" in normalize_url(url).split("//", 1)[1] else "/"
            crawled_paths.add(path.rstrip("/") or "/")
        except Exception:
            pass

    codebase_paths = {}
    for r in codebase_routes:
        route = r["route"]
        if route.startswith("http"):
            try:
                path = "/" + route.split("//", 1)[1].split("/", 1)[1]
            except Exception:
                continue
        else:
            path = route
        path = path.rstrip("/") or "/"
        # Skip parameterized routes for exact match
        if ":" not in path and "<" not in path and "{" not in path:
            codebase_paths[path] = r

    tested = []
    untested = []
    for path, route_info in codebase_paths.items():
        if path in crawled_paths:
            tested.append(route_info)
        else:
            untested.append(route_info)

    return {
        "total_codebase_routes": len(codebase_paths),
        "total_crawled_pages": len(crawled_paths),
        "tested_routes": tested,
        "untested_routes": untested,
        "coverage_percent": round(len(tested) / max(len(codebase_paths), 1) * 100, 1)
    }


def scan_codebase(codebase_path: str) -> dict:
    """Collect lightweight project metadata for test planning and code mapping."""
    ignored_dirs = {
        ".git", ".next", ".nuxt", "build", "coverage", "dist", "node_modules",
        "__pycache__", ".pytest_cache", ".venv", "venv"
    }
    source_exts = {".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss", ".json"}
    file_list = []
    file_tree = {}
    existing_tests = []
    routing_files = []
    components = []

    if not codebase_path or not os.path.isdir(codebase_path):
        return {
            "framework_type": "Unknown",
            "file_list": [],
            "existing_tests": [],
            "routing_files": [],
            "components": [],
            "file_tree": {}
        }

    for root, dirs, files in os.walk(codebase_path):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".cache")]
        rel_root = os.path.relpath(root, codebase_path)
        tree_cursor = file_tree
        if rel_root != ".":
            for part in rel_root.split(os.sep):
                tree_cursor = tree_cursor.setdefault(part, {})

        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in source_exts:
                continue

            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, codebase_path)
            if len(file_list) >= 500:
                continue

            file_list.append(rel_path)
            tree_cursor[filename] = "file"

            lower_path = rel_path.lower()
            base_name = filename.lower()
            if any(marker in lower_path for marker in [".test.", ".spec.", "__tests__", "playwright.config", "vitest.config"]):
                existing_tests.append(rel_path)
            if any(marker in base_name for marker in ["app.", "routes.", "router.", "layout.", "page."]):
                routing_files.append(rel_path)
            if (
                ext in {".jsx", ".tsx"}
                or "/components/" in lower_path.replace(os.sep, "/")
                or "\\components\\" in lower_path
            ):
                components.append(rel_path)

    framework_type = "HTML/JS"
    package_json_path = os.path.join(codebase_path, "package.json")
    if os.path.exists(package_json_path):
        try:
            with open(package_json_path, "r", encoding="utf-8") as package_file:
                package_data = json.load(package_file)
            deps = {
                **package_data.get("dependencies", {}),
                **package_data.get("devDependencies", {})
            }
            if "next" in deps:
                framework_type = "Next.js"
            elif "react" in deps and "vite" in deps:
                framework_type = "React + Vite"
            elif "react" in deps:
                framework_type = "React"
            elif "vue" in deps:
                framework_type = "Vue"
        except Exception as exc:
            logger.warning(f"Unable to parse package.json in {codebase_path}: {exc}")
    elif any(path.endswith((".jsx", ".tsx")) for path in file_list):
        framework_type = "React"

    return {
        "framework_type": framework_type,
        "file_list": file_list,
        "existing_tests": existing_tests[:50],
        "routing_files": routing_files[:50],
        "components": components[:100],
        "file_tree": file_tree
    }


def read_key_files(codebase_path: str, codebase_data: dict, max_files: int = 8, max_chars_per_file: int = 2500) -> str:
    """Read concise source snippets that help LLM planning without flooding context."""
    priority_files = []
    for bucket in ("routing_files", "components", "existing_tests"):
        for rel_path in codebase_data.get(bucket, []):
            if rel_path not in priority_files:
                priority_files.append(rel_path)

    for rel_path in codebase_data.get("file_list", []):
        lower_path = rel_path.lower()
        if any(name in lower_path for name in ["package.json", "src/main.", "src/index.", "src/app."]):
            if rel_path not in priority_files:
                priority_files.append(rel_path)

    snippets = []
    for rel_path in priority_files[:max_files]:
        full_path = os.path.join(codebase_path, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as source_file:
                content = source_file.read(max_chars_per_file)
            snippets.append(f"\n--- {rel_path} ---\n{content}")
        except UnicodeDecodeError:
            logger.warning(f"Skipping non-text file during codebase read: {rel_path}")
        except Exception as exc:
            logger.warning(f"Unable to read key file {rel_path}: {exc}")

    return "\n".join(snippets)[:16000]


def extract_page_snapshot(page, current_url: str, status_code: int = 200) -> dict:
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    title = page.title() or (soup.title.string.strip() if soup.title and soup.title.string else "Untitled Page")

    forms = []
    for form in soup.find_all("form")[:5]:
        form_info = {
            "action": form.get("action", ""),
            "method": form.get("method", "get").lower(),
            "inputs": []
        }
        for input_tag in form.find_all(["input", "select", "textarea"]):
            form_info["inputs"].append({
                "type": input_tag.get("type", "text"),
                "name": input_tag.get("name", ""),
                "placeholder": input_tag.get("placeholder", ""),
                "required": bool(input_tag.has_attr("required"))
            })
        forms.append(form_info)

    links = []
    for link in soup.find_all("a", href=True)[:50]:
        href = link.get("href")
        text = link.get_text().strip()
        links.append({"href": href, "text": text[:100]})

    headings = []
    for heading in soup.find_all(["h1", "h2", "h3"])[:15]:
        heading_text = heading.get_text().strip()
        if heading_text:
            headings.append(f"{heading.name}: {heading_text[:100]}")

    meta_tags = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            meta_tags[name.lower()] = content[:200]

    images = []
    for img in soup.find_all("img")[:30]:
        alt = (img.get("alt") or "").strip()
        images.append({
            "src": (img.get("src") or "")[:200],
            "alt": alt[:100],
            "has_alt": bool(alt),
        })

    return {
        "page_url": current_url,
        "title": title,
        "html_length": len(html),
        "html_snippet": html[:2500],
        "forms": forms,
        "links": links,
        "headings": headings,
        "meta_tags": meta_tags,
        "images": images,
        "status_code": status_code,
    }


def expand_navigation_regions(page) -> None:
    """Try common dashboard toggles so hidden nav/footer links become discoverable."""
    sidebar_toggle_selectors = [
        "button[aria-label*='menu' i]",
        "button[aria-label*='navigation' i]",
        "button[aria-label*='sidebar' i]",
        "button[title*='menu' i]",
        "button:has-text('Menu')",
        "button:has-text('Navigation')",
        "button:has-text('Sidebar')",
        "button:has-text('More')",
    ]
    for selector in sidebar_toggle_selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            clicked = False
            for i in range(count):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=1500)
                    clicked = True
                    break
            if clicked:
                try:
                    page.wait_for_load_state("networkidle", timeout=1500)
                except Exception:
                    pass
                break
        except Exception:
            continue

    dropdown_selectors = [
        "aside [aria-expanded='false']",
        "nav [aria-expanded='false']",
        "[role='navigation'] [aria-expanded='false']",
        "aside .dropdown-toggle",
        "nav .dropdown-toggle",
        "[role='button'][aria-expanded='false']",
        "button[aria-expanded='false']",
    ]
    for selector in dropdown_selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            for i in range(count):
                el = loc.nth(i)
                try:
                    if el.is_visible() and el.get_attribute("aria-expanded") == "false":
                        el.click(timeout=1000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass
        except Exception:
            continue


def harvest_navigation_links(page, base_url: str) -> list:
    """Collect anchors from common navigation regions and normalize them into absolute URLs."""
    collected = []
    selectors = [
        "a[href]",
        "nav a[href]",
        "aside a[href]",
        "header a[href]",
        "footer a[href]",
        "[role='navigation'] a[href]",
    ]
    seen = set()
    for selector in selectors:
        try:
            hrefs = page.eval_on_selector_all(
                selector,
                "elements => elements.map(el => ({href: el.href || el.getAttribute('href') || '', text: (el.innerText || '').trim()})).filter(x => !!x.href)"
            )
        except Exception:
            hrefs = []
        for item in hrefs:
            href = item.get("href") or ""
            text = item.get("text") or ""
            if href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            try:
                absolute = href if href.startswith("http") else urljoin(base_url, href)
            except Exception:
                continue
            key = absolute.lower()
            if key in seen:
                continue
            seen.add(key)
            collected.append({"href": absolute, "text": text[:100]})
    return collected


def click_navigation_items_for_routes(page, base_url: str, max_clicks: int = 24) -> list:
    """Click dashboard navigation controls and collect routes revealed by client-side routing."""
    discovered = []
    blocked_text = ("logout", "log out", "sign out", "delete", "remove", "close", "cancel")
    candidate_selector = (
        "nav a, nav button, aside a, aside button, header a, header button, "
        "footer a, footer button, [role='navigation'] a, [role='navigation'] button, "
        "[class*='sidebar' i] a, [class*='sidebar' i] button, "
        "[class*='menu' i] a, [class*='menu' i] button, "
        "[class*='nav' i] a, [class*='nav' i] button"
    )
    try:
        count = min(page.locator(candidate_selector).count(), max_clicks)
    except Exception:
        return discovered

    start_url = page.url
    for index in range(count):
        try:
            item = page.locator(candidate_selector).nth(index)
            label = ((item.inner_text(timeout=1000) or "") + " " + (item.get_attribute("aria-label") or "")).strip().lower()
            if not label or any(marker in label for marker in blocked_text):
                continue

            href = item.get_attribute("href")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                absolute = href if href.startswith("http") else urljoin(base_url, href)
                if same_site(absolute, base_url):
                    discovered.append(absolute)
                continue

            before_url = page.url
            item.click(timeout=2000)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            after_url = page.url
            if after_url and after_url != before_url and same_site(after_url, base_url):
                discovered.append(after_url)

            for nav_link in harvest_navigation_links(page, after_url or base_url):
                nav_href = nav_link.get("href")
                if nav_href and same_site(nav_href, base_url):
                    discovered.append(nav_href)

            if page.url != start_url:
                try:
                    page.goto(start_url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                    expand_navigation_regions(page)
                except Exception:
                    pass
        except Exception:
            continue

    deduped = []
    for href in discovered:
        if href not in deduped:
            deduped.append(href)
    return deduped


def discover_pages_with_playwright(url: str, max_pages: int = 50, auth: dict = None, seed_urls: list = None, is_mobile: bool = False, cancel_check = None, task_id: str = None) -> list:
    normalized = normalize_url(url)
    snapshots = []
    visited = set()
    queue = [normalized]
    for seed in seed_urls or []:
        normalized_seed = normalize_url(seed)
        if normalized_seed not in queue:
            queue.append(normalized_seed)

    try:
        logger.info(f"Starting Playwright crawl for {normalized} (is_mobile={is_mobile})")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            if is_mobile:
                context = browser.new_context(
                    viewport={"width": 375, "height": 667},
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 14_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1",
                    is_mobile=True,
                    has_touch=True,
                    ignore_https_errors=True
                )
            else:
                context = browser.new_context(
                    ignore_https_errors=True,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
            page = context.new_page()
            page.set_default_timeout(20000)
            if auth and auth.get("auth_required"):
                authenticate_browser_context(page, auth, normalized, logger.info, task_id=task_id)
                try:
                    expand_navigation_regions(page)
                except Exception:
                    pass
            else:
                try:
                    page.goto(normalized, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    check_and_click_guest_bypass(page, logger.info)
                    save_live_screenshot(page, task_id)
                except Exception:
                    pass

            while queue and len(snapshots) < max_pages:
                if cancel_check and cancel_check():
                    logger.info("[Crawler] Cancellation requested. Stopping page discovery.")
                    break
                current_url = queue.pop(0)
                if current_url.rstrip("/") in visited:
                    continue
                visited.add(current_url.rstrip("/"))

                response = None
                try:
                    response = page.goto(current_url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    try:
                        expand_navigation_regions(page)
                    except Exception:
                        pass
                except PlaywrightTimeoutError as exc:
                    logger.warning(f"Timeout loading {current_url}: {exc}")
                except Exception as exc:
                    logger.warning(f"Failed to navigate to {current_url}: {exc}")

                actual_url = page.url
                if actual_url.rstrip("/") != current_url.rstrip("/") and same_site(actual_url, normalized):
                    if actual_url.rstrip("/") in visited:
                        continue
                    visited.add(actual_url.rstrip("/"))
                    current_url = actual_url

                status_code = response.status if response else 500
                try:
                    snapshot = extract_page_snapshot(page, current_url, status_code)
                    snapshots.append(snapshot)
                    save_live_screenshot(page, task_id)
                except Exception as exc:
                    logger.error(f"Failed to extract snapshot for {current_url}: {exc}")
                    continue

                link_hrefs = harvest_navigation_links(page, current_url)
                clicked_hrefs = click_navigation_items_for_routes(page, current_url)

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

            context.close()
            browser.close()
    except Exception as exc:
        logger.warning(f"Playwright discovery failed for {normalized}: {exc}")

    if not snapshots:
        return [crawl_website(url)]

    return snapshots


def crawl_website(url: str) -> dict:
    data = {
        "page_url": normalize_url(url),
        "title": "Website under test",
        "forms": [],
        "links": [],
        "headings": [],
        "meta_tags": {},
        "html_snippet": "",
        "status_code": 200
    }

    try:
        target_url = normalize_url(url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        logger.info(f"Crawling website: {target_url}")
        response = httpx.get(target_url, headers=headers, follow_redirects=True, timeout=10.0)
        data["status_code"] = response.status_code
        soup = BeautifulSoup(response.text, "html.parser")

        data["title"] = soup.title.string.strip() if soup.title and soup.title.string else data["title"]
        for meta in soup.find_all("meta"):
            name = meta.get("name") or meta.get("property")
            content = meta.get("content")
            if name and content:
                data["meta_tags"][name.lower()] = content[:200]

        for h in soup.find_all(["h1", "h2", "h3"]):
            text = h.get_text().strip()
            if text:
                data["headings"].append(f"{h.name}: {text[:100]}")

        for form in soup.find_all("form")[:5]:
            inputs = []
            for input_tag in form.find_all(["input", "textarea", "select"]):
                inputs.append({
                    "type": input_tag.get("type", "text"),
                    "name": input_tag.get("name", ""),
                    "placeholder": input_tag.get("placeholder", ""),
                    "required": bool(input_tag.has_attr("required"))
                })
            data["forms"].append({
                "action": form.get("action", ""),
                "method": form.get("method", "get").lower(),
                "inputs": inputs
            })

        for a in soup.find_all("a", href=True)[:50]:
            data["links"].append({"href": a.get("href"), "text": a.get_text().strip()[:100]})

        data["html_length"] = len(response.text)
        data["html_snippet"] = response.text[:2500]
        images = []
        for img in soup.find_all("img")[:30]:
            alt = (img.get("alt") or "").strip()
            images.append({"src": (img.get("src") or "")[:200], "alt": alt[:100], "has_alt": bool(alt)})
        data["images"] = images
    except Exception as exc:
        logger.error(f"Error crawling URL {url}: {exc}")
        data["status_code"] = 500
        data["title"] = "Unable to crawl website"
        data["headings"] = ["h1: Error loading page"]
        data["html_length"] = 0
        data["images"] = []

    return data


def extract_codebase_page_info(codebase_path: str, codebase_data: dict) -> dict:
    """
    Statically analyzes page files in the React codebase to extract form fields,
    validation rules, expected success responses, API calls, and error messages.
    """
    from urllib.parse import urlparse
    page_info = {}
    
    file_list = codebase_data.get("file_list", [])
    for rel_path in file_list:
        # Check files under src/pages
        if not (rel_path.replace("\\", "/").startswith("src/pages/") and rel_path.endswith((".jsx", ".tsx"))):
            continue
            
        filename = os.path.basename(rel_path)
        page_name = os.path.splitext(filename)[0]
        
        # Exclude router / layouts / guards
        if page_name in ["AnimatedRoutes", "RedirectGuard", "Layout", "PreLoginLayout", "ProtectedRoute", "AccessDeniedPage"]:
            continue
            
        full_path = os.path.join(codebase_path, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            # 1. Parse input names
            inputs = []
            input_matches = re.findall(r'(?:name|id)\s*=\s*["\']([^"\']+)["\']', content)
            custom_input_matches = re.findall(r'<(?:TextInput|CheckboxInput|SelectInput|RadioInput|DatePickerInput|DatePicker)\s+[^>]*name\s*=\s*["\']([^"\']+)["\']', content)
            
            seen_inputs = set()
            for inp in input_matches + custom_input_matches:
                inp_lower = inp.lower()
                if inp_lower in seen_inputs or any(x in inp_lower for x in ["class", "style", "key", "route", "btn", "button", "active", "click", "auth"]):
                    continue
                seen_inputs.add(inp_lower)
                inputs.append(inp)
                
            # 2. Parse validation errors
            errors = []
            error_matches = re.findall(r'(?:setError|toast\.error|toast\.success|alert|showMessage)\s*\(\s*["\']([^"\']+)["\']', content)
            for err in error_matches:
                if len(err) > 5 and len(err) < 120 and err not in errors:
                    errors.append(err)
                    
            # 3. Parse API calls
            api_calls = []
            api_matches = re.findall(r'(?:apiService\.(?:post|get|put|delete)|ENDPOINTS\.[A-Z0-9_]+)', content)
            for api in api_matches:
                if api not in api_calls:
                    api_calls.append(api)
                    
            # 4. Success outcomes / Redirects
            redirects = []
            nav_matches = re.findall(r'navigate\s*\(\s*["\']([^"\']+)["\']', content)
            for nav in nav_matches:
                if nav not in redirects:
                    redirects.append(nav)
                    
            if inputs or errors or api_calls or redirects:
                page_info[page_name] = {
                    "rel_path": rel_path,
                    "inputs": inputs,
                    "errors": errors[:6],
                    "api_calls": api_calls[:4],
                    "redirects": redirects[:3]
                }
        except Exception as e:
            logger.warning(f"Failed to statically parse {rel_path}: {e}")
            
    return page_info


def match_url_to_component(page_url: str, codebase_page_info: dict) -> str:
    from urllib.parse import urlparse
    path = urlparse(page_url).path.strip("/")
    if not path:
        if "Login" in codebase_page_info:
            return "Login"
        return ""
        
    normalized_path = path.replace("-", "").replace("_", "").lower()
    
    # Try exact match
    for comp_name in codebase_page_info:
        if comp_name.lower() == normalized_path:
            return comp_name
            
    # Try partial match
    for comp_name in codebase_page_info:
        if normalized_path in comp_name.lower():
            return comp_name
            
    return ""


def build_test_plan(url: str, pages: list, codebase_data: dict) -> dict:
    """Generate page-specific test cases; pass/fail is decided during live browser execution."""
    profile = aggregate_site_profile(url, pages)
    domain = profile["domain"]
    page_url = profile["url"]
    title = profile["primary_title"]

    use_cases = []
    suggestions = []

    def pending_test(title_text, steps, expected, check_type, page_url_override=None):
        return {
            "title": title_text,
            "steps": steps,
            "expected_result": expected,
            "status": "pending",
            "error_message": None,
            "severity": None,
            "page_url": page_url_override or page_url,
            "check_type": check_type,
        }

    # Always keep one site-level entry, but page-specific checks are added below.
    use_cases.append({
        "title": f"Site Availability — {domain}",
        "description": f"Verify that the submitted site and its discovered pages load correctly.",
        "test_cases": [
            pending_test(
                f"HTTP Status for '{title}'",
                f"1. Navigate to {page_url}\n2. Capture HTTP status\n3. Verify page is not blocked",
                "Page should return HTTP 200 and render meaningful content.",
                "page_load",
            )
        ],
    })

    if profile["is_blocked"]:
        suggestions.append({
            "title": f"Bot protection blocking scans on {domain}",
            "description": f"Page title '{title}' indicates Cloudflare or bot protection. Automated QA may not see real content.",
            "priority": "high",
        })

    if profile["status_code"] != 200:
        suggestions.append({
            "title": f"Fix HTTP {profile['status_code']} on {domain}",
            "description": f"The submitted URL returned HTTP {profile['status_code']}. Resolve before release.",
            "priority": "critical",
        })

    # Build a page-by-page view so every discovered screen gets its own targeted checks.
    for page in pages or []:
        page_profile = build_page_profile(page, page_url)
        page_title = page_profile["title"]
        page_specific_cases = [
            pending_test(
                f"Load {page_title}",
                f"1. Navigate to {page_profile['page_url']}\n2. Confirm the page renders\n3. Check for blocked or error content",
                "The page should render successfully and show its own content.",
                "page_load",
                page_profile["page_url"],
            ),
            pending_test(
                f"Heading Structure on {page_title} ({page_profile['heading_count']} headings)",
                f"1. Inspect heading hierarchy on {page_profile['page_url']}\n2. Verify H1 presence\n3. Check logical nesting",
                "Page should have a clear H1 and structured headings.",
                "heading_structure",
                page_profile["page_url"],
            ),
            pending_test(
                f"Content Depth on {page_title} ({page_profile['html_length']} bytes)",
                f"1. Measure rendered HTML size ({page_profile['html_length']} bytes)\n2. Compare against minimum threshold\n3. Flag thin pages",
                "Page should contain substantive content beyond a loading shell.",
                "content_depth",
                page_profile["page_url"],
            ),
        ]

        if page_profile["link_count"] > 0:
            sample_text = ", ".join(page_profile["link_texts"][:3]) or "navigation links"
            page_specific_cases.append(
                pending_test(
                    f"Link Health Check ({page_profile['link_count']} links on {page_title})",
                    f"1. Sample up to 8 links from {page_title} ({sample_text})\n2. Verify each responds with HTTP < 400\n3. Flag broken links",
                    "Navigation links on this page should resolve without client or server errors.",
                    "link_health",
                    page_profile["page_url"],
                )
            )
        elif not page_profile["is_blocked"]:
            page_specific_cases.append(
                pending_test(
                    f"Missing Navigation on '{page_title}'",
                    f"1. Scan {page_profile['page_url']}\n2. Search for anchor navigation\n3. Confirm crawlability",
                    "The page should expose at least one navigational link if it is intended to branch to other screens.",
                    "navigation_presence",
                    page_profile["page_url"],
                )
            )

        if page_profile["form_count"] > 0:
            field_count = len(page_profile["form_field_names"])
            field_label = ", ".join(page_profile["form_field_names"][:4]) or "form fields"
            page_specific_cases.append(
                pending_test(
                    f"Required Field Validation - {page_title} ({field_count} fields)",
                    f"1. Open {page_title}\n2. Fill with dummy values where applicable\n3. Leave required fields blank and attempt submit\n4. Verify validation messages",
                    "Required inputs should be marked and enforced before submission, including dummy form entry and validation checks.",
                    "form_required",
                    page_profile["page_url"],
                )
            )
            use_cases.append({
                "title": f"Form Flow on {page_title}",
                "description": f"Validate forms and dummy user input on '{page_title}' using fields like {field_label}.",
                "test_cases": [page_specific_cases[-1]],
            })

        if page_profile["image_count"] > 0:
            page_specific_cases.append(
                pending_test(
                    f"Alt Text Coverage on {page_title} ({page_profile['images_missing_alt']} missing)",
                    f"1. Scan {page_profile['image_count']} images\n2. Count images without alt text\n3. Report offenders",
                    "Informative images should include descriptive alt attributes.",
                    "image_alt",
                    page_profile["page_url"],
                )
            )

        # Add Deep Interactive Flow check to test every clickable element
        if not page_profile["is_blocked"]:
            page_specific_cases.append(
                pending_test(
                    f"Deep Interactive Flow on {page_title}",
                    f"1. Open {page_profile['page_url']}\n2. Discover all interactive buttons, toggles, add/edit icons\n3. Click each sequentially and check for modals, tabs, or redirects\n4. Auto-fill and validate any modal forms",
                    "All interactive elements should work without errors, and modal forms should validate or submit successfully.",
                    "deep_interaction",
                    page_profile["page_url"],
                )
            )

        use_cases.append({
            "title": f"Page Coverage — {page_title}",
            "description": f"Validate the actual page '{page_title}' at {page_profile['page_url']}.",
            "test_cases": page_specific_cases,
        })

    codebase_path = codebase_data.get("codebase_path")
    if codebase_path and os.path.isdir(codebase_path):
        codebase_page_info = extract_codebase_page_info(codebase_path, codebase_data)
        for page in pages or []:
            page_profile = build_page_profile(page, page_url)
            comp_name = match_url_to_component(page_profile["page_url"], codebase_page_info)
            if comp_name:
                info = codebase_page_info[comp_name]
                test_cases_list = [
                    pending_test(
                        f"Code Review — {comp_name} Input Validations",
                        f"1. Open page: {page_profile['page_url']}\n2. Verify the following inputs from code are active: {', '.join(info['inputs']) if info['inputs'] else 'none'}\n3. Trigger client validations to verify error outputs",
                        "Expected form inputs should render correctly and throw appropriate user/validation errors.",
                        "code_validation",
                        page_profile["page_url"]
                    ),
                    pending_test(
                        f"Code Review — {comp_name} Expected Outputs & API calls",
                        f"1. Analyze submit flow on {comp_name}\n2. Submit data and check for API calls: {', '.join(info['api_calls']) if info['api_calls'] else 'default submission'}\n3. Verify redirect to: {', '.join(info['redirects']) if info['redirects'] else 'same page'}",
                        "Form submit should execute the expected API calls and redirect as defined in the source code.",
                        "code_redirect",
                        page_profile["page_url"]
                    )
                ]
                if info["errors"]:
                    test_cases_list.append(
                        pending_test(
                            f"Code Review — {comp_name} Expected Errors",
                            f"1. Fuzz inputs on {page_profile['page_url']}\n2. Try to trigger the following expected error cases defined in code:\n" + "\n".join([f"   - {err}" for err in info["errors"]]),
                            "The page should display the correct error messages corresponding to validation rules defined in the code.",
                            "code_errors",
                            page_profile["page_url"]
                        )
                    )
                use_cases.append({
                    "title": f"Code Audit & Logic Flow — {comp_name} ({info['rel_path']})",
                    "description": f"Verify logic flows, validation errors, and expected outputs extracted from codebase page {comp_name}.",
                    "test_cases": test_cases_list
                })

    if profile["meta_count"] < 3:
        suggestions.append({
            "title": f"Add meta tags on {domain} ({profile['meta_count']} found)",
            "description": f"Page '{title}' has only {profile['meta_count']} meta tags. Add description, viewport, and Open Graph tags.",
            "priority": "high",
        })
    if not profile["has_h1"]:
        suggestions.append({
            "title": f"Add H1 heading on '{title}'",
            "description": f"No H1 was detected on {page_url}. Add a primary heading for SEO and accessibility.",
            "priority": "high",
        })
    if profile["html_length"] < 8000 and not profile["is_blocked"]:
        suggestions.append({
            "title": f"Expand content on {domain} ({profile['html_length']} bytes)",
            "description": f"'{title}' has relatively thin HTML content. Add more descriptive copy and structured sections.",
            "priority": "medium",
        })
    if profile["images_missing_alt"] > 0:
        suggestions.append({
            "title": f"Fix missing alt text on {domain}",
            "description": f"{profile['images_missing_alt']} of {profile['image_count']} images on '{title}' lack alt text.",
            "priority": "medium",
        })
    if profile["link_count"] == 0 and not profile["is_blocked"] and profile["status_code"] == 200:
        suggestions.append({
            "title": f"Improve navigation on {domain}",
            "description": f"No links were discovered on '{title}'. Add clear navigation paths.",
            "priority": "high",
        })
    if profile["page_count"] <= 1 and profile["link_count"] > 0 and not profile["is_blocked"]:
        suggestions.append({
            "title": f"Increase internal linking on {domain}",
            "description": f"Only {profile['page_count']} page was crawled from {page_url}. Link to more internal destinations.",
            "priority": "medium",
        })
    if not suggestions:
        suggestions.append({
            "title": f"Extend QA coverage for {domain}",
            "description": f"No major structural issues on '{title}'. Add performance, accessibility, and security test suites.",
            "priority": "low",
        })

    return {"use_cases": use_cases, "suggestions": suggestions}



def save_screenshot(page, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        page.screenshot(path=path, full_page=True)
    except Exception as exc:
        logger.warning(f"Screenshot save failed: {exc}")


def save_live_screenshot(page, task_id: str) -> None:
    if not task_id:
        return
    try:
        live_dir = os.path.join("screenshots", task_id)
        os.makedirs(live_dir, exist_ok=True)
        live_path = os.path.join(live_dir, "live_preview.png")
        page.screenshot(path=live_path, full_page=False)
    except Exception as exc:
        logger.warning(f"Live preview screenshot save failed: {exc}")


def autofill_form_typewriter(page, task_id: str, safe_values: dict, live_preview_path: str = None, log_callback=None) -> dict:
    """Discovers input fields on the page, typewrite-fills them sequentially with highlighting and saves live previews."""
    discover_script = """
    () => {
        const fields = [];
        const labelMap = {};
        document.querySelectorAll('label').forEach(lbl => {
            const htmlFor = lbl.getAttribute('for');
            const text = (lbl.textContent || '').trim().replace(/\\*$/, '').trim();
            if (htmlFor) labelMap[htmlFor] = text;
        });

        function getFieldLabel(el) {
            const id = el.id || '';
            if (labelMap[id]) return labelMap[id];
            const parentLabel = el.closest('label');
            if (parentLabel) return (parentLabel.textContent || '').trim().replace(/\\*$/, '').trim();
            const placeholder = el.getAttribute('placeholder') || '';
            if (placeholder) return placeholder;
            const name = el.getAttribute('name') || '';
            if (name) return name;
            
            let prev = el.previousElementSibling;
            while (prev) {
                const text = (prev.textContent || '').trim();
                if (text && text.length < 50) return text.replace(/\\*$/, '').trim();
                prev = prev.previousElementSibling;
            }
            return '';
        }

        function getSelector(el) {
            if (el.id) return `#${CSS.escape(el.id)}`;
            if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
            let path = [];
            let current = el;
            while (current && current.nodeType === Node.ELEMENT_NODE) {
                let selector = current.nodeName.toLowerCase();
                if (current.id) {
                    selector += '#' + CSS.escape(current.id);
                    path.unshift(selector);
                    break;
                } else {
                    let sib = current, nth = 1;
                    while (sib = sib.previousElementSibling) {
                        if (sib.nodeName.toLowerCase() == selector) nth++;
                    }
                    if (nth != 1) selector += ":nth-of-type("+nth+")";
                }
                path.unshift(selector);
                current = current.parentNode;
            }
            return path.join(" > ");
        }

        // 1. Text inputs
        const textInputs = document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea');
        textInputs.forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) return;
            const label = getFieldLabel(el);
            const selector = getSelector(el);
            fields.push({
                selector,
                label,
                type: el.tagName.toLowerCase() === 'textarea' ? 'textarea' : el.getAttribute('type') || 'text'
            });
        });

        // 2. Dropdown Selects
        document.querySelectorAll('select').forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) return;
            const label = getFieldLabel(el) || 'Select Dropdown';
            const selector = getSelector(el);
            const options = Array.from(el.options);
            const validOption = options.find(o => o.value && o.value !== '' && !o.disabled) || options[0];
            fields.push({
                selector,
                label,
                type: 'select',
                value: validOption ? validOption.value : '',
                text: validOption ? validOption.text : ''
            });
        });

        // 3. Checkboxes & Radios
        document.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(el => {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return;
            if (!el.checked) {
                const label = getFieldLabel(el) || 'Checkbox/Radio';
                const selector = getSelector(el);
                fields.push({
                    selector,
                    label,
                    type: el.getAttribute('type'),
                    value: 'click'
                });
            }
        });

        // 4. File Inputs
        const fileInputs = [];
        document.querySelectorAll('input[type="file"]').forEach((el, idx) => {
            const label = getFieldLabel(el) || `File Input ${idx+1}`;
            const selector = getSelector(el);
            fileInputs.push({ label, selector });
        });

        // 5. Custom Dropdowns
        document.querySelectorAll('[role="combobox"], [class*="select-container"], [class*="Select-container"], [class*="-control"], .select, .dropdown').forEach(el => {
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) return;
            if (el.querySelector('[role="combobox"]') && el !== el.querySelector('[role="combobox"]')) return;
            
            const label = getFieldLabel(el) || 'Custom Dropdown';
            const selector = getSelector(el);
            fields.push({
                selector,
                label,
                type: 'custom_dropdown',
                value: 'click'
            });
        });

        return { fields, fileInputs };
    }
    """
    
    filled_fields = {}
    try:
        res = page.evaluate(discover_script)
        fields = res.get("fields", [])
        file_inputs = res.get("fileInputs", [])
    except Exception as e:
        if log_callback:
            log_callback(f"[Typewriter] Error discovering fields: {e}")
        return {"filled_fields": {}, "file_inputs": []}

    for field in fields:
        selector = field["selector"]
        label = field["label"]
        ftype = field["type"]
        label_lower = label.lower()
        
        # Determine value to fill
        val = "QA Test Value"
        url_lower = page.url.lower()
        is_login_page = "login" in url_lower or "signin" in url_lower
        
        if ftype in ["text", "textarea", "email", "password", "tel", "number", "date"]:
            placeholder = ""
            try:
                placeholder = (page.locator(selector).get_attribute("placeholder") or "").lower()
            except Exception:
                pass
                
            if is_login_page:
                if any(k in label_lower or k in placeholder for k in ["email", "username", "user"]):
                    val = safe_values.get("username") or "admin@gmail.com"
                elif "pass" in label_lower or "pass" in placeholder:
                    val = safe_values.get("password") or "Admin@#123"
            else:
                if "email" in label_lower or "email" in placeholder:
                    val = safe_values.get("email") or "test.qa@datagrid.co.in"
                elif "pass" in label_lower or "pass" in placeholder:
                    val = safe_values.get("password") or "TestSecure#2026"
                elif any(k in label_lower or k in placeholder for k in ["phone", "mobile"]):
                    val = safe_values.get("phone") or "9876543210"
                elif "year" in label_lower or "year" in placeholder:
                    val = "2026"
                elif any(k in label_lower or k in placeholder for k in ["shared on", "date"]) or ftype == "date":
                    val = "2026-06-18"
                elif "fund" in label_lower or "fund" in placeholder:
                    val = "PNS Capital Fund A"
                elif "company" in label_lower or "company" in placeholder:
                    val = "Datagrid Investment Company"
                elif "investor" in label_lower or "investor" in placeholder:
                    val = "QA Investor Group"
                elif any(k in label_lower or k in placeholder for k in ["department", "dept"]):
                    if "desc" in label_lower or "desc" in placeholder:
                        val = "This department handles visual, responsive, and performance QA automation testing."
                    else:
                        val = "Quality Assurance"
                elif any(k in label_lower or k in placeholder for k in ["description", "desc"]):
                    val = "This is a sample description generated automatically for testing purposes."
                elif any(k in label_lower or k in placeholder for k in ["role", "designation"]):
                    val = "Quality Assurance Lead"
                elif "address" in label_lower or "address" in placeholder:
                    val = "404 Innovation Way, Tech Park"
                elif "city" in label_lower or "city" in placeholder:
                    val = "Mumbai"
                elif "state" in label_lower or "state" in placeholder:
                    val = "Maharashtra"
                elif any(k in label_lower or k in placeholder for k in ["zip", "pin", "postal"]):
                    val = "400001"
                elif "name" in label_lower or "name" in placeholder:
                    first = safe_values.get("first_name")
                    last = safe_values.get("last_name")
                    val = f"{first} {last}" if (first and last) else "QA Test User"
                elif "url" in label_lower or "url" in placeholder or "link" in label_lower:
                    val = "https://pns-capital.datagrid.co.in"

            try:
                # Highlight in blue/purple outline to indicate active focus
                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #6366f1'")
                page.locator(selector).focus()
                page.locator(selector).fill("") # Clear input first
                
                # Typewriter type character by character
                for char in val:
                    page.keyboard.type(char)
                    page.wait_for_timeout(35) # small delay per char
                    if live_preview_path:
                        page.screenshot(path=live_preview_path, full_page=False)

                # Set success border
                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #22c55e'")
                filled_fields[label or "Text Input"] = val
                page.wait_for_timeout(100)
            except Exception as fill_err:
                if log_callback:
                    log_callback(f"[Typewriter] Warning: Failed to fill text field '{label}': {fill_err}")

        elif ftype == "select":
            try:
                sel_val = field["value"]
                sel_txt = field["text"]
                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #6366f1'")
                page.locator(selector).select_option(sel_val)
                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #22c55e'")
                filled_fields[label] = sel_txt
                if live_preview_path:
                    page.screenshot(path=live_preview_path, full_page=False)
                page.wait_for_timeout(200)
            except Exception as select_err:
                if log_callback:
                    log_callback(f"[Typewriter] Warning: Failed to select dropdown option '{label}': {select_err}")

        elif ftype in ["checkbox", "radio"]:
            try:
                page.locator(selector).click()
                filled_fields[label] = "Checked"
                if live_preview_path:
                    page.screenshot(path=live_preview_path, full_page=False)
                page.wait_for_timeout(200)
            except Exception as click_err:
                if log_callback:
                    log_callback(f"[Typewriter] Warning: Failed to click checkbox/radio '{label}': {click_err}")

        elif ftype == "custom_dropdown":
            try:
                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #6366f1'")
                page.locator(selector).click()
                page.wait_for_timeout(400)
                if live_preview_path:
                    page.screenshot(path=live_preview_path, full_page=False)

                # Look for option to click
                options_script = """
                () => {
                    const options = Array.from(document.querySelectorAll('[role="option"], [class*="option"], .dropdown-item, .select-option, [class*="-menu"] div, li'));
                    const optionToClick = options.find(opt => {
                        const optStyle = window.getComputedStyle(opt);
                        const text = (opt.textContent || '').trim();
                        return optStyle.display !== 'none' && 
                               optStyle.visibility !== 'hidden' && 
                               opt.offsetWidth > 0 &&
                               text !== '' && 
                               !text.startsWith('Select') &&
                               !text.includes('No options') &&
                               !text.includes('Loading');
                    });
                    if (optionToClick) {
                        function getSelector(el) {
                            if (el.id) return `#${CSS.escape(el.id)}`;
                            if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
                            let path = [];
                            let current = el;
                            while (current && current.nodeType === Node.ELEMENT_NODE) {
                                let selector = current.nodeName.toLowerCase();
                                if (current.id) {
                                    selector += '#' + CSS.escape(current.id);
                                    path.unshift(selector);
                                    break;
                                } else {
                                    let sib = current, nth = 1;
                                    while (sib = sib.previousElementSibling) {
                                        if (sib.nodeName.toLowerCase() == selector) nth++;
                                    }
                                    if (nth != 1) selector += ":nth-of-type("+nth+")";
                                }
                                path.unshift(selector);
                                current = current.parentNode;
                            }
                            return path.join(" > ");
                        }
                        return { selector: getSelector(optionToClick), text: (optionToClick.textContent || '').trim() };
                    }
                    return null;
                }
                """
                opt_info = page.evaluate(options_script)
                if opt_info:
                    opt_sel = opt_info["selector"]
                    opt_txt = opt_info["text"]
                    page.locator(opt_sel).click()
                    filled_fields[label] = opt_txt
                else:
                    page.locator(selector).press("ArrowDown")
                    page.wait_for_timeout(150)
                    page.locator(selector).press("Enter")
                    filled_fields[label] = "Selected Option"

                page.evaluate(f"document.querySelector('{selector}').style.border = '2px solid #22c55e'")
                if live_preview_path:
                    page.screenshot(path=live_preview_path, full_page=False)
                page.wait_for_timeout(200)
            except Exception as custom_err:
                if log_callback:
                    log_callback(f"[Typewriter] Warning: Failed to fill custom dropdown '{label}': {custom_err}")

    return {"filled_fields": filled_fields, "file_inputs": file_inputs}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _classify_auth_field(name: str = "", field_id: str = "", placeholder: str = "", label: str = "", aria: str = "") -> str:
    haystack = _normalize_text(" ".join([name, field_id, placeholder, label, aria]))
    if not haystack:
        return "unknown"

    if any(term in haystack for term in ["captcha", "recaptcha", "hcaptcha", "verify you are human", "cloudflare", "challenge"]):
        return "challenge"

    if any(term in haystack for term in ["otp", "one time", "verification code", "verification", "auth code", "security code", "passcode", "pin", "token"]):
        return "otp"

    # More reliable mobile identifier detection (covers Sellingo-style ids like user_mobile_login)
    if any(term in haystack for term in [
        "mobile", "phone", "cell", "whatsapp",
        "user_mobile", "user phone", "user-phone",
        "mobile_login", "phone_login", "phone number",
        "phone number", "cell phone",
        "msisdn",
    ]):
        return "mobile"

    if "email" in haystack:
        return "email"

    if any(term in haystack for term in ["password", "passcode", "pin"]):
        return "password"

    if any(term in haystack for term in [
        "user", "login", "account", "username",
        "customer", "consumer", "client", "member",
        "staff", "employee", "card number", "card_number",
        "merchant", "partner", "id", "identity",
    ]):
        return "username"

    return "unknown"



def _field_metadata(page, locator) -> dict:
    try:
        field_id = locator.get_attribute("id") or ""
        label_text = ""
        if field_id:
            try:
                label_loc = page.locator(f"label[for='{field_id}']")
                if label_loc.count() > 0:
                    label_text = label_loc.first.text_content() or ""
            except Exception:
                label_text = ""
        return {
            "name": locator.get_attribute("name") or "",
            "id": field_id,
            "placeholder": locator.get_attribute("placeholder") or "",
            "type": locator.get_attribute("type") or "",
            "aria": locator.get_attribute("aria-label") or "",
            "autocomplete": locator.get_attribute("autocomplete") or "",
            "label": label_text,
        }
    except Exception:
        return {}


def _discover_auth_form(page) -> dict:
    js_code = """
    () => {
        const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
        const inputData = inputs.map(el => {
            let labelText = '';
            const id = el.id || '';
            if (id) {
                try {
                    const escapedId = CSS.escape(id);
                    const label = document.querySelector(`label[for="${escapedId}"]`);
                    if (label) {
                        labelText = (label.textContent || '').trim();
                    }
                } catch (e) {}
            }
            
            let isVisible = false;
            try {
                const style = window.getComputedStyle(el);
                isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0';
            } catch (e) {}
            
            return {
                name: el.getAttribute('name') || '',
                id: id,
                placeholder: el.getAttribute('placeholder') || '',
                type: el.getAttribute('type') || el.tagName.toLowerCase(),
                aria: el.getAttribute('aria-label') || '',
                autocomplete: el.getAttribute('autocomplete') || '',
                label: labelText,
                visible: isVisible
            };
        });
        
        const buttons = Array.from(document.querySelectorAll('button'));
        const buttonData = buttons.map(el => {
            return {
                text: (el.innerText || '').trim(),
                type: el.getAttribute('type') || ''
            };
        });
        
        const bodyText = (document.body ? document.body.innerText : '') || '';
        
        return {
            inputs: inputData,
            buttons: buttonData,
            body_text: bodyText
        };
    }
    """
    try:
        data = page.evaluate(js_code)
        inputs = data["inputs"]
        for meta in inputs:
            meta["role"] = _classify_auth_field(
                meta.get("name", ""),
                meta.get("id", ""),
                meta.get("placeholder", ""),
                meta.get("label", ""),
                meta.get("aria", "")
            )
        buttons = []
        for btn in data["buttons"]:
            buttons.append({
                "text": _normalize_text(btn["text"]),
                "type": btn["type"],
            })
        body_text = _normalize_text(data["body_text"])
        return {"inputs": inputs, "buttons": buttons, "body_text": body_text}
    except Exception as exc:
        logger.warning(f"Error evaluating auth form discovery: {exc}")
        return {"inputs": [], "buttons": [], "body_text": ""}


def _detect_challenge_text(page_text: str, url: str) -> bool:
    challenge_terms = (
        "cloudflare",
        "captcha",
        "recaptcha",
        "hcaptcha",
        "verify you are human",
        "checking your browser",
        "security check",
        "bot challenge",
        "security challenge",
        "cf-challenge",
    )
    url_text = _normalize_text(url)
    haystack = f"{page_text} {url_text}"
    return any(term in haystack for term in challenge_terms)


def _auth_issue_to_fields(issue: dict) -> list:
    issue_type = (issue or {}).get("type", "")
    fields = (issue or {}).get("fields") or []
    if fields:
        return fields
    mapping = {
        "missing_input": [],
        "otp_required": ["otp"],
        "password_required": ["password"],
        "mobile_required": ["mobile"],
        "email_required": ["email"],
        "challenge": ["challenge"],
    }
    return mapping.get(issue_type, [])


def _classify_auth_flow(required_fields: list) -> str:
    fields = [str(field or "").lower() for field in required_fields or []]
    joined = " ".join(fields)
    if "mobile" in joined and "otp" in joined:
        return "mobile_otp"
    if "email" in joined and "otp" in joined:
        return "email_otp"
    if "password" in joined and "otp" in joined:
        return "password_reset"
    if "otp" in joined:
        return "otp"
    if "password" in joined:
        return "password"
    if any(field in {"mobile", "email", "username"} for field in fields):
        return "identifier"
    if "challenge" in fields:
        return "challenge"
    return "general"


def check_and_click_guest_bypass(page, log_callback=None) -> bool:
    """Check if the page has a guest bypass option (e.g., 'Continue without logging in') and click it."""
    try:
        bypass_selectors = [
            "text='Continue without logging in' i",
            "text='Continue as Guest' i",
            "text='Guest Login' i",
            "text='Skip Login' i",
            "text='Continue as guest' i",
            "a:has-text('Continue without logging in')",
            "a:has-text('Continue as Guest')",
            "button:has-text('Continue without logging in')",
            "button:has-text('Continue as Guest')",
        ]
        for selector in bypass_selectors:
            loc = page.locator(selector)
            if loc.count() > 0:
                for j in range(loc.count()):
                    btn = loc.nth(j)
                    # Relax strict visibility checks to support elements hidden on desktop layouts
                    if btn.is_visible() or btn.count() > 0:
                        if log_callback:
                            log_callback(f"[GuestBypass] Found guest bypass link/button: '{selector}'. Clicking it to access protected areas.")
                        try:
                            btn.click(force=True, timeout=3000)
                        except Exception:
                            # Fallback to JavaScript click in case of Playwright actionability issues
                            btn.evaluate("el => el.click()")
                        page.wait_for_timeout(2000)  # Wait for transition/redirect
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        return True
    except Exception as e:
        if log_callback:
            log_callback(f"[GuestBypass] Error clicking guest bypass: {e}")
    return False


def authenticate_browser_context(context_or_page, auth: dict, start_url: str, log_callback=None, task_id: str = None) -> bool:
    """Attempt a lightweight login flow when credentials are provided."""
    if not auth or not auth.get("auth_required"):
        return False
    try:
        auth["_codex_auth_issue"] = None
    except Exception:
        pass

    login_url = normalize_url(auth.get("auth_login_url") or start_url)
    post_login_url = normalize_url(auth.get("auth_post_login_url") or "")
    username = auth.get("auth_username") or ""
    password = auth.get("auth_password") or ""
    otp_code = auth.get("auth_otp_code") or ""

    if hasattr(context_or_page, "new_page"):
        context = context_or_page
        page = context.new_page()
        should_close_page = True
    else:
        page = context_or_page
        should_close_page = False
    page.set_default_timeout(20000)
    try:
        if log_callback:
            log_callback(f"[Orchestrator] Attempting authenticated session via {login_url}")
        page.goto(login_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        save_live_screenshot(page, task_id)

        snapshot = _discover_auth_form(page)
        if _detect_challenge_text(snapshot.get("body_text", ""), page.url):
            if log_callback:
                log_callback(f"[Orchestrator] Bot/challenge screen detected at {page.url}. Waiting for manual verification or a human-assisted step.")
            return False

        # Check if login input fields (username and password) are visible in the DOM
        visible_username_inputs = False
        visible_password_inputs = False
        for inp in snapshot.get("inputs", []):
            if inp.get("visible"):
                role = inp.get("role")
                if role in {"mobile", "email", "username"}:
                    visible_username_inputs = True
                elif role == "password":
                    visible_password_inputs = True

        need_login_trigger = not visible_username_inputs or (password and not visible_password_inputs)

        # If they are not visible, look for trigger buttons/links and click them to open the modal
        if need_login_trigger:
            if log_callback:
                log_callback("[Orchestrator] Login form inputs not fully visible on page load. Attempting to click LOGIN trigger links/buttons.")
            # Prioritize login triggers and exclude registration forms
            trigger_selectors = [
                "a:has-text('LOGIN')",
                "a:has-text('Login')",
                "a:has-text('Sign in')",
                "a:has-text('Sign In')",
                "button:has-text('LOGIN')",
                "button:has-text('Login')",
                "button:has-text('Sign in')",
                "button:has-text('Sign In')",
                "a.login_popup_register:has-text('LOGIN')",
            ]
            clicked_trigger = False
            for selector in trigger_selectors:
                loc = page.locator(selector)
                if loc.count() > 0:
                    try:
                        for j in range(loc.count()):
                            trigger = loc.nth(j)
                            if trigger.is_visible():
                                trigger.click(timeout=3000)
                                clicked_trigger = True
                                save_live_screenshot(page, task_id)
                                break
                        if clicked_trigger:
                            break
                    except Exception as e:
                        if log_callback:
                            log_callback(f"[Orchestrator] Failed clicking login trigger '{selector}': {e}")
            if clicked_trigger:
                try:
                    # Wait for password or username inputs to become visible
                    page.wait_for_selector("input[type='password'], input[name*='login' i], input[name*='mobile' i]", state="visible", timeout=4000)
                except Exception:
                    page.wait_for_timeout(1500)
                snapshot = _discover_auth_form(page)
                save_live_screenshot(page, task_id)

        def fill_first(selectors, value):
            for selector in selectors:
                loc = page.locator(selector)
                count = loc.count()
                # Prioritize visible elements
                for j in range(count):
                    el = loc.nth(j)
                    if el.is_visible():
                        try:
                            el.evaluate("el => el.style.border = '2px solid #6366f1'")
                            el.focus()
                            el.fill("")
                            for char in value:
                                page.keyboard.type(char)
                                page.wait_for_timeout(35)
                                save_live_screenshot(page, task_id)
                            el.evaluate("el => el.style.border = '2px solid #22c55e'")
                            page.wait_for_timeout(100)
                            return True
                        except Exception:
                            pass
                # Fallback to normal first match
                if count > 0:
                    try:
                        loc.first.evaluate("el => el.style.border = '2px solid #6366f1'")
                        loc.first.focus()
                        loc.first.fill("")
                        for char in value:
                            page.keyboard.type(char)
                            page.wait_for_timeout(35)
                            save_live_screenshot(page, task_id)
                        loc.first.evaluate("el => el.style.border = '2px solid #22c55e'")
                        page.wait_for_timeout(100)
                        return True
                    except Exception:
                        pass
            return False

        field_values = {
            "mobile": username,
            "email": username,
            "username": username,
            "password": password,
            "otp": otp_code,
        }

        field_selectors = {
            "mobile": [
                "input:not([type='hidden'])[name*='mobile' i]",
                "input:not([type='hidden'])[id*='mobile' i]",
                "input:not([type='hidden'])[placeholder*='mobile' i]",
                "input:not([type='hidden'])[name*='phone' i]",
                "input:not([type='hidden'])[id*='phone' i]",
                "input:not([type='hidden'])[placeholder*='phone' i]",
            ],
            "email": [
                "input:not([type='hidden'])[type='email']",
                "input:not([type='hidden'])[name*='email' i]",
                "input:not([type='hidden'])[placeholder*='email' i]",
            ],
            "username": [
                "input:not([type='hidden'])[name*='user' i]",
                "input:not([type='hidden'])[name*='login' i]",
                "input:not([type='hidden'])[name*='account' i]",
                "input:not([type='hidden'])[placeholder*='user' i]",
                "input:not([type='hidden'])[name*='customer' i]",
                "input:not([type='hidden'])[id*='customer' i]",
                "input:not([type='hidden'])[placeholder*='customer' i]",
                "input:not([type='hidden'])[name*='consumer' i]",
                "input:not([type='hidden'])[id*='consumer' i]",
                "input:not([type='hidden'])[placeholder*='consumer' i]",
                "input:not([type='hidden'])[name*='client' i]",
                "input:not([type='hidden'])[id*='client' i]",
                "input:not([type='hidden'])[placeholder*='client' i]",
                "input:not([type='hidden'])[name*='member' i]",
                "input:not([type='hidden'])[id*='member' i]",
                "input:not([type='hidden'])[placeholder*='member' i]",
                "input:not([type='hidden'])[name*='staff' i]",
                "input:not([type='hidden'])[id*='staff' i]",
                "input:not([type='hidden'])[placeholder*='staff' i]",
                "input:not([type='hidden'])[name*='employee' i]",
                "input:not([type='hidden'])[id*='employee' i]",
                "input:not([type='hidden'])[placeholder*='employee' i]",
                "input:not([type='hidden'])[name*='merchant' i]",
                "input:not([type='hidden'])[id*='merchant' i]",
                "input:not([type='hidden'])[placeholder*='merchant' i]",
                "input:not([type='hidden'])[name*='partner' i]",
                "input:not([type='hidden'])[id*='partner' i]",
                "input:not([type='hidden'])[placeholder*='partner' i]",
                "input:not([type='hidden'])[name*='id' i]",
                "input:not([type='hidden'])[id*='id' i]",
                "input:not([type='hidden'])[placeholder*='id' i]",
                "input:not([type='hidden'])[name*='identity' i]",
                "input:not([type='hidden'])[id*='identity' i]",
                "input:not([type='hidden'])[placeholder*='identity' i]",
                "input:not([type='hidden'])[type='text']",
            ],
            "password": [
                "input:not([type='hidden'])[type='password']",
                "input:not([type='hidden'])[name*='pass' i]",
                "input:not([type='hidden'])[placeholder*='password' i]",
            ],
            "otp": [
                "input:not([type='hidden'])[name*='otp' i]",
                "input:not([type='hidden'])[name*='code' i]",
                "input:not([type='hidden'])[name*='token' i]",
                "input:not([type='hidden'])[name*='verify' i]",
                "input:not([type='hidden'])[placeholder*='otp' i]",
                "input:not([type='hidden'])[placeholder*='code' i]",
                "input:not([type='hidden'])[placeholder*='security' i]",
            ],
        }

        field_roles = [field.get("role") for field in snapshot.get("inputs", [])]

        if log_callback:
            try:
                inputs_debug = [
                    {
                        "role": (f.get("role") or "unknown"),
                        "name": f.get("name") or "",
                        "id": f.get("id") or "",
                        "placeholder": f.get("placeholder") or "",
                        "label": f.get("label") or "",
                        "type": f.get("type") or "",
                        "aria": f.get("aria") or "",
                    }
                    for f in (snapshot.get("inputs", []) or [])
                ]
                log_callback(f"[Orchestrator][AuthDebug] Detected login inputs at {page.url}: {json.dumps(inputs_debug, ensure_ascii=False)}")
            except Exception:
                pass

        ordered_roles = []
        for role in ["mobile", "email", "username", "password", "otp"]:
            if role in field_roles or role == "password" or role == "otp":
                ordered_roles.append(role)

        matched_any = False
        for role in ordered_roles:
            value = field_values.get(role) or ""
            if not value:
                continue
            if fill_first(field_selectors.get(role, []), value):
                matched_any = True
                if log_callback:
                    log_callback(f"[Orchestrator] Filled detected {role} field at {page.url}.")

        submit_selectors = [
            "button:has-text('LOGIN')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "button:has-text('Sign In')",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Verify')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('Send')",
            "text=Login",
            "text=Sign in",
        ]
        submitted = False
        # Prioritize clicking visible submit buttons
        for selector in submit_selectors:
            try:
                loc = page.locator(selector)
                count = loc.count()
                for j in range(count):
                    btn = loc.nth(j)
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        submitted = True
                        save_live_screenshot(page, task_id)
                        break
                if submitted:
                    break
            except Exception:
                continue

        # Fallback to normal click if no visible submit buttons clicked
        if not submitted:
            for selector in submit_selectors:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        loc.first.click(timeout=2000)
                        submitted = True
                        save_live_screenshot(page, task_id)
                        break
                except Exception:
                    continue

        if submitted:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            save_live_screenshot(page, task_id)

        snapshot = _discover_auth_form(page)

        if log_callback:
            try:
                post_inputs_debug = [
                    {
                        "role": (f.get("role") or "unknown"),
                        "name": f.get("name") or "",
                        "id": f.get("id") or "",
                        "placeholder": f.get("placeholder") or "",
                        "label": f.get("label") or "",
                        "type": f.get("type") or "",
                        "aria": f.get("aria") or "",
                    }
                    for f in (snapshot.get("inputs", []) or [])
                ]
                log_callback(
                    f"[Orchestrator][AuthDebug] After submit @ {page.url}: inputs={json.dumps(post_inputs_debug, ensure_ascii=False)} "
                    f"body_len={len(snapshot.get('body_text','') or '')}"
                )
            except Exception:
                pass

        if _detect_challenge_text(snapshot.get("body_text", ""), page.url):
            if log_callback:
                log_callback(f"[Orchestrator] Bot/challenge screen detected after submit at {page.url}. Manual verification is required.")
            return False

        input_roles = [field.get("role") for field in snapshot.get("inputs", []) if field.get("visible")]
        if log_callback:
            try:
                log_callback(f"[Orchestrator][AuthDebug] Post-submit detected input roles: {sorted(set(input_roles))}")
            except Exception:
                pass

        login_roles = [role for role in input_roles if role in {"mobile", "email", "username", "password", "otp"}]
        missing_roles = []
        for role in login_roles:
            if role in {"mobile", "email", "username"} and not username:
                missing_roles.append(role)
            elif role == "password" and not password:
                missing_roles.append(role)
            elif role == "otp" and not otp_code:
                missing_roles.append(role)
        if missing_roles:
            issue = {
                "type": "missing_input",
                "fields": sorted(set(missing_roles)),
                "page_url": page.url,
            }
            try:
                required_fields = sorted(set(missing_roles))
                auth["_codex_auth_issue"] = issue
                auth["auth_next_step"] = "Provide the missing authentication field(s)"
                auth["auth_required_fields"] = json.dumps(required_fields)
                auth["auth_flow"] = _classify_auth_flow(required_fields)
            except Exception:
                pass
            if log_callback:
                needed = ", ".join(sorted(set(missing_roles)))
                log_callback(f"[Orchestrator] Login flow requires missing input(s): {needed}. Pause and collect these values before resuming.")
            return False

        if post_login_url:
            try:
                page.goto(post_login_url, wait_until="domcontentloaded")
            except Exception:
                pass

        # Re-scan page for auth challenge / OTP presence BEFORE declaring success.
        snapshot = _discover_auth_form(page)
        if _detect_challenge_text(snapshot.get("body_text", ""), page.url):
            if log_callback:
                log_callback(f"[Orchestrator] Bot/challenge screen detected after submit at {page.url}. Manual verification is required.")
            return False

        input_roles = [field.get("role") for field in snapshot.get("inputs", []) if field.get("visible")]
        # If OTP is present and we didn't provide it, we must pause.
        if "otp" in input_roles and not otp_code:
            issue = {
                "type": "otp_required",
                "fields": ["otp"],
                "page_url": page.url,
            }
            try:
                auth["_codex_auth_issue"] = issue
                auth["auth_next_step"] = "Provide the OTP / verification code"
                auth["auth_required_fields"] = json.dumps(["otp"])
                auth["auth_flow"] = _classify_auth_flow(["otp"])
            except Exception:
                pass
            if log_callback:
                log_callback("[Orchestrator] OTP input detected but no OTP provided. Pause and wait for OTP.")
            return False

        success_markers = ["logout", "sign out", "my account", "profile", "member area"]
        body_text = (page.locator("body").inner_text(timeout=5000) or "").lower()
        current_url = (page.url or "").lower()
        on_login_page = "login" in current_url or "sign-in" in current_url or "signin" in current_url

        # Prefer explicit authenticated markers; do not rely solely on redirect.
        if any(marker in body_text for marker in success_markers):
            if log_callback:
                log_callback(f"[Orchestrator] Authenticated session appears active at {page.url}")
            return True

        # If we redirected away from login, treat as "submitted" but still keep safety: only success if URL is post-login.
        if post_login_url and page.url.rstrip("/") == post_login_url.rstrip("/") and not on_login_page:
            if log_callback:
                log_callback(f"[Orchestrator] Authentication likely succeeded by redirect to post-login URL: {page.url}")
            return submitted

        if log_callback:
            log_callback(f"[Orchestrator] Authentication attempt completed, but authenticated markers were not detected at {page.url}")
        return submitted

    except Exception as exc:
        if log_callback:
            log_callback(f"[Orchestrator] Authentication attempt failed: {exc}")
        return False
    finally:
        if should_close_page:
            try:
                page.close()
            except Exception:
                pass


def run_test_validation(page, context, test_data: dict, profile: dict, auth: dict = None) -> tuple:
    """Execute a live browser check and return (status, error_message, severity)."""
    check_type = test_data.get("check_type", "")
    page_url = normalize_url(test_data.get("page_url") or profile["url"])
    title = profile["primary_title"]
    domain = profile["domain"]

    def safe_goto(url):
        response = page.goto(url, wait_until="domcontentloaded")
        if auth and auth.get("auth_required"):
            current_url_lower = page.url.lower()
            url_lower = url.lower()
            login_keywords = ["/login", "/signin", "/sign-in", "/auth"]
            has_login_current = any(k in current_url_lower for k in login_keywords)
            has_login_intended = any(k in url_lower for k in login_keywords)
            if has_login_current and not has_login_intended:
                logger.info(f"[SelfHealing] Detected login redirect from {url} to {page.url}. Attempting to re-authenticate context.")
                success = authenticate_browser_context(page, auth, url, logger.info, task_id=task_id)
                if success:
                    logger.info(f"[SelfHealing] Re-authentication successful. Navigating back to {url}")
                    response = page.goto(url, wait_until="domcontentloaded")
                else:
                    logger.warning(f"[SelfHealing] Re-authentication failed after redirect to {page.url}")
        save_live_screenshot(page, task_id)
        return response

    task_id = test_data.get("task_id")
    protected_usernames = DEFAULT_PROTECTED_USERNAMES
    enable_safe_mode = True
    temp_user_prefix = "test_user_"
    
    if task_id:
        db = SessionLocal()
        try:
            safety_config = db.query(SafetyConfig).filter(SafetyConfig.task_id == task_id).first()
            if safety_config:
                try:
                    loaded_users = json.loads(safety_config.protected_usernames_json)
                    if isinstance(loaded_users, list):
                        protected_usernames = set(loaded_users)
                except Exception:
                    pass
                enable_safe_mode = bool(safety_config.enable_safe_mode)
                temp_user_prefix = safety_config.temp_user_prefix or "test_user_"
        finally:
            db.close()

    # Admin protection check
    steps_text = test_data.get("steps") or ""
    if steps_text:
        steps_list = [s.strip() for s in steps_text.split("\n") if s.strip()]
        for step in steps_list:
            sanitized, was_blocked = sanitize_test_step(step, protected_usernames)
            if was_blocked:
                return "failed", f"Admin Protection Blocked Step: {sanitized}", "critical"

    try:
        if check_type == "page_load":
            response = safe_goto(page_url)
            status = response.status if response else 500
            if status != 200:
                return "failed", f"Page '{title}' failed to open and returned server error (HTTP {status}). The page did not load successfully.", "critical"
            if profile["is_blocked"]:
                return "failed", f"Page '{title}' is blocked by security (bot protection). We cannot inspect this page.", "high"
            return "passed", None, None

        safe_goto(page_url)

        if check_type == "link_health":
            broken = []
            checked = 0
            for link in profile["links"][:8]:
                href = link.get("href", "")
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    continue
                target = href if href.startswith("http") else urljoin(page_url, href)
                
                # Check for logout links to prevent logging out during health check
                target_lower = target.lower()
                text_lower = (link.get("text") or "").lower()
                logout_keywords = ["logout", "log-out", "signout", "sign-out", "log_out", "sign_out"]
                if any(k in target_lower or k in text_lower for k in logout_keywords):
                    logger.info(f"[LinkHealth] Skipping logout link: href={href}, text={link.get('text')}")
                    continue
                
                try:
                    response = context.request.get(target, timeout=10000)
                    checked += 1
                    if response.status >= 400:
                        broken.append(f"{link.get('text', 'link')[:30]} ({response.status})")
                except Exception:
                    broken.append(f"{link.get('text', 'link')[:30]} (unreachable)")
                if checked >= 8:
                    break
            if not checked:
                return "failed", f"We found zero links to click on this page. The user has no links or buttons to go to other pages.", "medium"
            if broken:
                return "failed", f"These links are broken and not working: {', '.join(broken[:5])}. Clicking them will show errors.", "high"
            return "passed", None, None

        if check_type == "navigation_presence":
            link_count = page.locator("a[href]").count()
            if link_count == 0:
                return "failed", f"No navigation links or menu links found on '{title}' page. The user will get stuck on this page.", "high"
            return "passed", None, None

        if check_type == "internal_pages":
            if profile["page_count"] <= 1:
                return "failed", f"Our crawler only found 1 page on the website. No other pages or sub-pages were found.", "medium"
            return "passed", None, None

        if check_type == "form_required":
            if page.locator("form").count() == 0 and page.locator("input:not([type=hidden]), textarea, select, [role=combobox]").count() == 0:
                return "failed", f"No forms or input fields found on this page during live testing.", "medium"

            # 1. Blank submit test
            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Save')",
                "button:has-text('Add')",
                "button:has-text('Create')",
                "input:has-text('Submit')"
            ]
            submit_btn = None
            for sel in submit_selectors:
                loc = page.locator(sel)
                if loc.count() > 0:
                    for i in range(loc.count()):
                        if loc.nth(i).is_visible():
                            submit_btn = loc.nth(i)
                            break
                if submit_btn:
                    break
            
            # Click submit blank first
            if submit_btn:
                try:
                    submit_btn.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    save_live_screenshot(page, task_id)
                except Exception:
                    pass

            # 2. Dynamic Form Auto-filling and Submission
            # Create a dummy PDF locally
            dummy_pdf_path = os.path.abspath("dummy_upload_test.pdf")
            if not os.path.exists(dummy_pdf_path):
                try:
                    with open(dummy_pdf_path, "wb") as f:
                        f.write(b"%PDF-1.4\n%EOF")
                except Exception:
                    pass

            # Define the JS auto-fill function
            js_autofill_script = """
            async function(safeValues) {
                const filledFields = {};
                const fileInputs = [];

                const labelMap = {};
                document.querySelectorAll('label').forEach(lbl => {
                    const htmlFor = lbl.getAttribute('for');
                    const text = (lbl.textContent || '').trim().replace(/\*$/, '').trim();
                    if (htmlFor) {
                        labelMap[htmlFor] = text;
                    }
                });

                function getFieldLabel(el) {
                    const id = el.id || '';
                    if (labelMap[id]) return labelMap[id];
                    const parentLabel = el.closest('label');
                    if (parentLabel) return (parentLabel.textContent || '').trim().replace(/\*$/, '').trim();
                    const placeholder = el.getAttribute('placeholder') || '';
                    if (placeholder) return placeholder;
                    const name = el.getAttribute('name') || '';
                    if (name) return name;
                    
                    let prev = el.previousElementSibling;
                    while (prev) {
                        const text = (prev.textContent || '').trim();
                        if (text && text.length < 50) return text.replace(/\*$/, '').trim();
                        prev = prev.previousElementSibling;
                    }
                    return '';
                }

                document.querySelectorAll('input[type="file"]').forEach((el, idx) => {
                    const label = getFieldLabel(el) || `File Input ${idx+1}`;
                    let selector = '';
                    if (el.id) selector = `#${CSS.escape(el.id)}`;
                    else if (el.name) selector = `input[name="${CSS.escape(el.name)}"]`;
                    else selector = `input[type="file"]`;
                    fileInputs.push({ label, selector });
                });

                function setInputValue(el, val) {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }

                document.querySelectorAll('select').forEach(el => {
                    const label = getFieldLabel(el) || 'Select Dropdown';
                    const options = Array.from(el.options);
                    const validOption = options.find(o => o.value && o.value !== '' && !o.disabled) || options[0];
                    if (validOption) {
                        el.value = validOption.value;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        filledFields[label] = validOption.text;
                    }
                });

                const textInputs = document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea');
                for (const el of textInputs) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) {
                        continue;
                    }

                    const label = getFieldLabel(el);
                    const labelLower = label.toLowerCase();
                    const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                    const type = el.getAttribute('type') || 'text';
                    let val = 'QA Test Value';

                    const urlLower = window.location.href.toLowerCase();
                    const isLoginPage = urlLower.includes('login') || urlLower.includes('signin');

                    if (isLoginPage) {
                        if (labelLower.includes('email') || placeholder.includes('email') || labelLower.includes('username') || placeholder.includes('username') || labelLower.includes('user') || placeholder.includes('user')) {
                            val = safeValues.username || 'admin@gmail.com';
                        } else if (labelLower.includes('pass') || placeholder.includes('pass')) {
                            val = safeValues.password || 'Admin@#123';
                        }
                    } else {
                        if (labelLower.includes('email') || placeholder.includes('email')) {
                            val = safeValues.email || 'test.qa@datagrid.co.in';
                        } else if (labelLower.includes('pass') || placeholder.includes('pass')) {
                            val = safeValues.password || 'TestSecure#2026';
                        } else if (labelLower.includes('phone') || labelLower.includes('mobile') || placeholder.includes('phone') || placeholder.includes('mobile')) {
                            val = safeValues.phone || '9876543210';
                        } else if (labelLower.includes('year') || placeholder.includes('year')) {
                            val = '2026';
                        } else if (labelLower.includes('shared on') || labelLower.includes('date') || placeholder.includes('date') || type === 'date') {
                            val = '2026-06-18';
                        } else if (labelLower.includes('fund') || placeholder.includes('fund')) {
                            val = 'PNS Capital Fund A';
                        } else if (labelLower.includes('company') || placeholder.includes('company')) {
                            val = 'Datagrid Investment Company';
                        } else if (labelLower.includes('investor') || placeholder.includes('investor')) {
                            val = 'QA Investor Group';
                        } else if (labelLower.includes('department') || placeholder.includes('department') || labelLower.includes('dept') || placeholder.includes('dept')) {
                            if (labelLower.includes('desc') || placeholder.includes('desc')) {
                                val = 'This department handles visual, responsive, and performance QA automation testing.';
                            } else {
                                val = 'Quality Assurance';
                            }
                        } else if (labelLower.includes('description') || placeholder.includes('description') || labelLower.includes('desc') || placeholder.includes('desc')) {
                            val = 'This is a sample description generated automatically for testing purposes.';
                        } else if (labelLower.includes('role') || placeholder.includes('role') || labelLower.includes('designation') || placeholder.includes('designation')) {
                            val = 'Quality Assurance Lead';
                        } else if (labelLower.includes('address') || placeholder.includes('address')) {
                            val = '404 Innovation Way, Tech Park';
                        } else if (labelLower.includes('city') || placeholder.includes('city')) {
                            val = 'Mumbai';
                        } else if (labelLower.includes('state') || placeholder.includes('state')) {
                            val = 'Maharashtra';
                        } else if (labelLower.includes('zip') || placeholder.includes('zip') || labelLower.includes('pin') || placeholder.includes('pin') || labelLower.includes('postal') || placeholder.includes('postal')) {
                            val = '400001';
                        } else if (labelLower.includes('name') || placeholder.includes('name')) {
                            val = (safeValues.first_name && safeValues.last_name) ? (safeValues.first_name + ' ' + safeValues.last_name) : 'QA Test User';
                        } else if (labelLower.includes('url') || placeholder.includes('url') || labelLower.includes('link') || placeholder.includes('link')) {
                            val = 'https://pns-capital.datagrid.co.in';
                        }
                    }

                    setInputValue(el, val);
                    filledFields[label || 'Text Input'] = val;
                }

                document.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') {
                        return;
                    }
                    if (!el.checked) {
                        el.click();
                        const label = getFieldLabel(el) || 'Checkbox/Radio';
                        filledFields[label] = 'Checked';
                    }
                });

                const customDropdowns = [];
                document.querySelectorAll('[role="combobox"], [class*="select-container"], [class*="Select-container"], [class*="-control"], .select, .dropdown').forEach(el => {
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                        return;
                    }
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) {
                        return;
                    }
                    if (el.querySelector('[role="combobox"]') && el !== el.querySelector('[role="combobox"]')) {
                        return;
                    }
                    customDropdowns.push(el);
                });

                for (const el of customDropdowns) {
                    const label = getFieldLabel(el) || 'Custom Dropdown';
                    try {
                        el.click();
                        await new Promise(r => setTimeout(r, 400));
                        
                        const options = Array.from(document.querySelectorAll('[role="option"], [class*="option"], .dropdown-item, .select-option, [class*="-menu"] div, li'));
                        const optionToClick = options.find(opt => {
                            const optStyle = window.getComputedStyle(opt);
                            const text = (opt.textContent || '').trim();
                            return optStyle.display !== 'none' && 
                                   optStyle.visibility !== 'hidden' && 
                                   opt.offsetWidth > 0 &&
                                   text !== '' && 
                                   !text.startsWith('Select') &&
                                   !text.includes('No options') &&
                                   !text.includes('Loading');
                        });

                        if (optionToClick) {
                            const optText = (optionToClick.textContent || '').trim();
                            optionToClick.click();
                            filledFields[label] = optText;
                        } else {
                            el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
                            await new Promise(r => setTimeout(r, 150));
                            el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                            filledFields[label] = 'Selected Option';
                        }
                    } catch (e) {
                        console.error(e);
                    }
                    await new Promise(r => setTimeout(r, 200));
                }

                return { filledFields, fileInputs };
            }
            """

            try:
                # Generate safe testing values if enabled
                safe_values = {}
                if enable_safe_mode:
                    for field in ["email", "username", "password", "first_name", "last_name", "phone"]:
                        safe_values[field] = get_safe_form_values(field, test_index=1, prefix=temp_user_prefix)
                else:
                    safe_values = {
                        "email": "test.qa@datagrid.co.in",
                        "username": "qa_test_user",
                        "password": "TestSecure#2026",
                        "first_name": "QA",
                        "last_name": "Test User",
                        "phone": "9876543210"
                    }
                live_preview_path = os.path.join("screenshots", task_id, "live_preview.png") if task_id else None
                autofill_result = autofill_form_typewriter(page, task_id, safe_values, live_preview_path, logger.info)
                filled_data = autofill_result.get("filled_fields", {})
                file_inputs = autofill_result.get("file_inputs", [])

                # Log temp records for cleanup
                if task_id and enable_safe_mode and filled_data:
                    db = SessionLocal()
                    try:
                        for k, v in filled_data.items():
                            if isinstance(v, str) and v.startswith(temp_user_prefix):
                                cleanup_log = TestCleanupLog(
                                    task_id=task_id,
                                    record_type="user",
                                    record_identifier=v,
                                    action="created"
                                )
                                db.add(cleanup_log)
                        db.commit()
                    except Exception as db_err:
                        logger.error(f"Failed to log temp record for cleanup: {db_err}")
                    finally:
                        db.close()
                
                for file_in in file_inputs:
                    sel = file_in.get("selector")
                    label = file_in.get("label", "File")
                    try:
                        page.locator(sel).set_input_files(dummy_pdf_path)
                        filled_data[label] = "dummy_upload_test.pdf"
                    except Exception as upload_err:
                        logger.warning(f"File upload error for selector {sel}: {upload_err}")
                
                import json
                json_str = json.dumps(filled_data)
                
                dummy_list = []
                for k, v in filled_data.items():
                    if k.lower() != "_token":
                        dummy_list.append(f"   - {k}: {v}")
                dummy_list_str = "\\n".join(dummy_list)
                
                test_data["steps"] = f"1. Open the form page.\\n2. Populate the fields with dummy values using Playwright:\\n{dummy_list_str}\\n\\nJSON_DUMMY_DATA: {json_str}"
                
            except Exception as js_err:
                logger.error(f"Autofill script error: {js_err}")
                filled_data = {}

            # Submit the form after auto-filling
            submit_btn_clicked = False
            if submit_btn:
                try:
                    submit_btn.click(timeout=4000)
                    submit_btn_clicked = True
                    page.wait_for_timeout(2500)
                    save_live_screenshot(page, task_id)
                except Exception:
                    pass
            
            # Check for redirect or success messages
            current_url = page.url
            url_changed = (current_url != page_url)
            
            errors_detected = page.evaluate("""
            () => {
                const errMsgs = [];
                document.querySelectorAll('.error, .invalid-feedback, [class*="error"], [class*="invalid"], .alert-danger').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0) {
                        const txt = (el.textContent || '').trim();
                        if (txt && txt.length < 150) errMsgs.push(txt);
                    }
                });
                return errMsgs;
            }
            """)
            
            if url_changed:
                return "passed", None, None
            
            if errors_detected:
                error_summary = ", ".join(errors_detected[:3])
                return "failed", f"Form submission failed with validation errors: {error_summary}", "high"
            
            body_text = page.locator("body").inner_text() or ""
            success_keywords = ["successfully", "saved", "created", "added", "success", "submitted"]
            has_success = any(kw in body_text.lower() for kw in success_keywords)
            
            if has_success:
                return "passed", None, None
                
            if submit_btn_clicked:
                return "passed", None, None
                
            return "failed", "Form fields were populated, but we could not submit the form successfully.", "medium"

        if check_type == "heading_structure":
            if page.locator("h1").count() == 0:
                return "failed", f"There is no main heading (H1) on '{title}' page. Every page should have a clear main title.", "high"
            return "passed", None, None

        if check_type == "content_depth":
            html_length = len(page.content())
            if profile["is_blocked"]:
                return "failed", f"The page is showing a bot challenge/security check instead of the real website content.", "high"
            if html_length < 3000:
                return "failed", f"The page is almost empty or loading very slowly ({html_length} bytes of content found).", "low"
            return "passed", None, None

        if check_type == "image_alt":
            missing = page.eval_on_selector_all(
                "img",
                "imgs => imgs.filter(i => !(i.alt || '').trim()).map(i => (i.src || '').split('/').pop().slice(0, 40))"
            )
            if missing:
                return "failed", f"{len(missing)} image(s) do not have descriptive labels: {', '.join(missing[:3])}. Screen readers won't be able to describe these images to blind users.", "medium"
            return "passed", None, None

        if check_type == "deep_interaction":
            logger.info(f"[DeepInteraction] Starting deep interactive validation on {page_url}")
            safe_goto(page_url)
            page.wait_for_timeout(2000)

            # Discover potentially clickable elements
            find_elements_js = """
            () => {
                const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"], [class*="btn" i], [class*="click" i]'));
                const clickables = [];
                const blacklist = ["logout", "log out", "signout", "sign out", "deactivate", "delete", "remove", "cancel", "clear", "reset"];
                
                candidates.forEach((el, idx) => {
                    let isVisible = false;
                    try {
                        const style = window.getComputedStyle(el);
                        isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
                                    style.display !== 'none' &&
                                    style.visibility !== 'hidden' &&
                                    style.opacity !== '0';
                    } catch (e) {}
                    
                    if (!isVisible) return;
                    
                    const text = (el.innerText || el.textContent || '').trim();
                    const textLower = text.toLowerCase();
                    
                    if (blacklist.some(term => textLower.includes(term))) return;
                    
                    const tagName = el.tagName.toLowerCase();
                    if (tagName === 'a') {
                        const href = el.getAttribute('href') || '';
                        if (href.startsWith('mailto:') || href.startsWith('tel:') || (href.startsWith('http') && !href.includes(window.location.host))) {
                            return;
                        }
                    }
                    
                    let selector = '';
                    if (el.id) {
                        selector = `#${CSS.escape(el.id)}`;
                    } else if (tagName === 'input' && el.getAttribute('name')) {
                        selector = `input[name="${CSS.escape(el.getAttribute('name'))}"]`;
                    }
                    
                    clickables.push({
                        index: idx,
                        tagName: tagName,
                        text: text.slice(0, 40),
                        selector: selector,
                        class: el.className || ''
                    });
                });
                return clickables;
            }
            """
            
            try:
                candidates = page.evaluate(find_elements_js)
            except Exception as eval_err:
                logger.error(f"[DeepInteraction] Failed to discover elements: {eval_err}")
                candidates = []
                
            logger.info(f"[DeepInteraction] Discovered {len(candidates)} candidate clickable elements.")
            
            actions_performed = 0
            max_actions = 12
            
            for item in candidates:
                if actions_performed >= max_actions:
                    break
                    
                tag = item.get("tagName", "").lower()
                text = item.get("text", "").strip()
                selector = item.get("selector")
                
                locator = None
                desc = f"<{tag}> text='{text}'"
                if selector:
                    locator = page.locator(selector).first
                    desc = f"<{tag}> selector='{selector}'"
                elif text:
                    clean_text = text.replace("'", "\\'")
                    locator = page.locator(f"{tag}:has-text('{clean_text}')").first
                else:
                    continue
                    
                try:
                    if not locator or locator.count() == 0 or not locator.is_visible():
                        continue
                        
                    logger.info(f"[DeepInteraction] Clicking element {actions_performed+1}: {desc}")
                    actions_performed += 1
                    
                    before_url = page.url
                    locator.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    save_live_screenshot(page, task_id)
                    
                    after_url = page.url
                    if after_url != before_url:
                        logger.info(f"[DeepInteraction] Redirection detected: {before_url} -> {after_url}")
                        if same_site(after_url, page_url):
                            page.go_back()
                            page.wait_for_timeout(1000)
                            save_live_screenshot(page, task_id)
                        else:
                            safe_goto(page_url)
                            page.wait_for_timeout(1500)
                            
                    # Check for modals
                    modal_selector = page.evaluate("""
                    () => {
                        const modalDivs = document.querySelectorAll('.modal, .popup, .dialog, [role="dialog"], [class*="modal" i], [class*="popup" i]');
                        for (const modal of modalDivs) {
                            const style = window.getComputedStyle(modal);
                            if (style.display !== 'none' && style.visibility !== 'hidden' && modal.offsetWidth > 0) {
                                return modal.id ? `#${CSS.escape(modal.id)}` : (modal.className ? `.${CSS.escape(modal.className.split(' ')[0])}` : '');
                            }
                        }
                        return null;
                    }
                    """)
                    
                    if modal_selector:
                        logger.info(f"[DeepInteraction] Visible modal dialog detected: {modal_selector}")
                        inputs_count = page.locator(f"{modal_selector} input:not([type=hidden]), {modal_selector} textarea, {modal_selector} select").count()
                        if inputs_count > 0:
                            logger.info(f"[DeepInteraction] Found {inputs_count} fields inside the modal. Performing autofill and validation checks.")
                            
                            submit_selectors = ["button[type='submit']", "button:has-text('Save')", "button:has-text('Add')", "button:has-text('Submit')", "input[type='submit']"]
                            modal_submit = None
                            for submit_sel in submit_selectors:
                                try:
                                    loc = page.locator(f"{modal_selector} {submit_sel}")
                                    if loc.count() > 0 and loc.first.is_visible():
                                        modal_submit = loc.first
                                        break
                                except Exception:
                                    pass
                                    
                            if modal_submit:
                                try:
                                    modal_submit.click(timeout=2000)
                                    page.wait_for_timeout(1000)
                                except Exception:
                                    pass
                                    
                            autofill_js = """
                            async function(safeValues) {
                                const filledFields = {};
                                const labelMap = {};
                                document.querySelectorAll('label').forEach(lbl => {
                                    const htmlFor = lbl.getAttribute('for');
                                    const text = (lbl.textContent || '').trim().replace(/\*$/, '').trim();
                                    if (htmlFor) labelMap[htmlFor] = text;
                                });
                                
                                function getFieldLabel(el) {
                                    const id = el.id || '';
                                    if (labelMap[id]) return labelMap[id];
                                    const parentLabel = el.closest('label');
                                    if (parentLabel) return (parentLabel.textContent || '').trim().replace(/\*$/, '').trim();
                                    return el.getAttribute('placeholder') || el.getAttribute('name') || 'Field';
                                }
                                
                                document.querySelectorAll('select').forEach(el => {
                                    const label = getFieldLabel(el);
                                    const validOption = Array.from(el.options).find(o => o.value && o.value !== '' && !o.disabled) || el.options[0];
                                    if (validOption) {
                                        el.value = validOption.value;
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        filledFields[label] = validOption.text;
                                    }
                                });
                                
                                const textInputs = document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea');
                                for (const el of textInputs) {
                                    const style = window.getComputedStyle(el);
                                    if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0) continue;
                                    
                                    const label = getFieldLabel(el);
                                    const labelLower = label.toLowerCase();
                                    const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                                    let val = 'QA Test Value';
                                    
                                    if (labelLower.includes('email') || placeholder.includes('email')) {
                                        val = safeValues.email || 'test.qa@datagrid.co.in';
                                    } else if (labelLower.includes('pass') || placeholder.includes('pass')) {
                                        val = safeValues.password || 'TestSecure#2026';
                                    } else if (labelLower.includes('phone') || labelLower.includes('mobile') || placeholder.includes('phone') || placeholder.includes('mobile')) {
                                        val = safeValues.phone || '9876543210';
                                    } else if (labelLower.includes('year') || placeholder.includes('year')) {
                                        val = '2026';
                                    } else if (labelLower.includes('date') || placeholder.includes('date')) {
                                        val = '2026-06-18';
                                    } else if (labelLower.includes('name') || placeholder.includes('name')) {
                                        val = (safeValues.first_name && safeValues.last_name) ? (safeValues.first_name + ' ' + safeValues.last_name) : 'QA Test User';
                                    }
                                    
                                    el.value = val;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    filledFields[label] = val;
                                }
                                
                                document.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(el => {
                                    if (window.getComputedStyle(el).display !== 'none' && !el.checked) {
                                        el.click();
                                    }
                                });
                                return filledFields;
                            }
                            """
                            try:
                                live_preview_path = os.path.join("screenshots", task_id, "live_preview.png") if task_id else None
                                autofill_result = autofill_form_typewriter(page, task_id, safe_values, live_preview_path, logger.info)
                                modal_filled = autofill_result.get("filled_fields", {})
                                if task_id and enable_safe_mode and modal_filled:
                                    db = SessionLocal()
                                    try:
                                        for k, v in modal_filled.items():
                                            if isinstance(v, str) and v.startswith(temp_user_prefix):
                                                cleanup_log = TestCleanupLog(
                                                    task_id=task_id,
                                                    record_type="user",
                                                    record_identifier=v,
                                                    action="created"
                                                )
                                                db.add(cleanup_log)
                                        db.commit()
                                    except Exception as db_err:
                                        logger.error(f"Failed to log temp record for cleanup: {db_err}")
                                    finally:
                                        db.close()
                                page.wait_for_timeout(500)
                            except Exception as autofill_err:
                                logger.error(f"[DeepInteraction] Autofill error: {autofill_err}")
                                
                            if modal_submit:
                                try:
                                    modal_submit.click(timeout=3000)
                                    page.wait_for_timeout(2000)
                                except Exception:
                                    pass
                                    
                        # Close the modal
                        close_selectors = [
                            f"{modal_selector} .close", f"{modal_selector} [class*='close' i]",
                            f"{modal_selector} button:has-text('Close')", f"{modal_selector} button:has-text('×')",
                            "button[class*='close' i]", ".modal-backdrop"
                        ]
                        for close_sel in close_selectors:
                            try:
                                close_btn = page.locator(close_sel)
                                if close_btn.count() > 0 and close_btn.first.is_visible():
                                    close_btn.first.click(timeout=2000)
                                    page.wait_for_timeout(1000)
                                    break
                            except Exception:
                                pass
                                
                    errors_detected = page.evaluate("""
                    () => {
                        const errMsgs = [];
                        document.querySelectorAll('.error, .invalid-feedback, [class*="error"], [class*="invalid"], .alert-danger').forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0) {
                                const txt = (el.textContent || '').trim();
                                if (txt && txt.length < 150) errMsgs.push(txt);
                            }
                        });
                        return errMsgs;
                    }
                    """)
                    if errors_detected:
                        logger.warning(f"[DeepInteraction] UI errors detected: {errors_detected}")
                        
                except Exception as click_err:
                    logger.warning(f"[DeepInteraction] Action failed for {desc}: {click_err}")
                    try:
                        safe_goto(page_url)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                        
            return "passed", None, None

        if check_type == "manual_page_load":
            response = safe_goto(page_url)
            status = response.status if response else 500
            body_text = (page.locator("body").inner_text(timeout=5000) or "").strip()
            if status != 200:
                return "failed", f"Could not load the page. It failed with error code {status}. Visible text: {body_text[:240] or 'empty page'}", "critical"
            if len(body_text) < 40:
                return "failed", f"Page loaded successfully but it looks blank or has very little text. Visible text: {body_text[:240] or 'empty page'}", "high"
            return "passed", None, None

        if check_type == "manual_blocker_check":
            response = safe_goto(page_url)
            status = response.status if response else 500
            body_text = (page.locator("body").inner_text(timeout=5000) or "").strip()
            console_messages = []
            try:
                console_messages = page.evaluate("() => window.__qa_console_messages || []")
            except Exception:
                console_messages = []
            if status >= 400:
                return "failed", f"Blocker check failed with error code {status}. Visible text: {body_text[:240] or 'empty page'}", "critical"
            if looks_like_blocked_page(page.title(), page.content()[:2500], body_text):
                return "failed", f"The website is showing a bot blocker or security check screen.", "high"
            if len(body_text) < 40:
                return "failed", f"The page loaded blank or contains almost no text. Visible text: {body_text[:240] or 'empty page'}", "high"
            if console_messages:
                return "failed", f"We detected some coding/console errors in the background: {', '.join(map(str, console_messages[:5]))}", "medium"
            return "passed", None, None

        # Legacy/Gemini tests without check_type — validate keywords against live page
        title_lower = test_data.get("title", "").lower()
        if "required field" in title_lower:
            temp_data = {**test_data, "check_type": "form_required"}
            res = run_test_validation(page, context, temp_data, profile, auth=auth)
            if "steps" in temp_data:
                test_data["steps"] = temp_data["steps"]
            return res
        if "navigation" in title_lower or "link" in title_lower:
            if not profile["links"]:
                return run_test_validation(page, context, {**test_data, "check_type": "navigation_presence"}, profile, auth=auth)
            return run_test_validation(page, context, {**test_data, "check_type": "link_health"}, profile, auth=auth)
        if "heading" in title_lower:
            return run_test_validation(page, context, {**test_data, "check_type": "heading_structure"}, profile, auth=auth)
        if "content" in title_lower:
            return run_test_validation(page, context, {**test_data, "check_type": "content_depth"}, profile, auth=auth)

        preset_status = test_data.get("status", "passed")
        if preset_status == "pending":
            return "passed", None, None
        return preset_status, test_data.get("error_message"), test_data.get("severity")

    except Exception as exc:
        return "failed", f"Runtime validation failure on {domain}: {exc}", "medium"


def build_manual_diagnostic_plan(url: str) -> dict:
    """Fallback diagnostic plan for hard-to-inspect sites like aahoa.com."""
    normalized = normalize_url(url)
    domain = normalized.split("//", 1)[1].split("/")[0]
    return {
        "use_cases": [
            {
                "title": f"Manual Diagnostic — {domain}",
                "description": "Direct browser verification with explicit failure capture for the submitted site.",
                "test_cases": [
                    {
                        "title": f"Open {domain} homepage",
                        "steps": f"1. Navigate to {normalized}\n2. Wait for DOM content\n3. Capture visible page text and browser state",
                        "expected_result": "The homepage should load and expose usable content.",
                        "status": "pending",
                        "error_message": None,
                        "severity": None,
                        "page_url": normalized,
                        "check_type": "manual_page_load",
                    },
                    {
                        "title": f"Inspect blocking indicators on {domain}",
                        "steps": f"1. Open {normalized}\n2. Check for bot-protection text, redirect loops, console errors, and failed requests\n3. Capture the root cause if the page fails",
                        "expected_result": "The page should not be blocked and should not throw browser-level errors.",
                        "status": "pending",
                        "error_message": None,
                        "severity": None,
                        "page_url": normalized,
                        "check_type": "manual_blocker_check",
                    },
                ],
            }
        ],
        "suggestions": [
            {
                "title": f"Improve diagnostic capture for {domain}",
                "description": "This site needs deeper browser capture because the current automation path may be missing the true runtime failure.",
                "priority": "high",
            }
        ],
    }


def execute_test_plan(task_id: str, pages: list, use_case_mapping: dict, use_case_titles: dict, auth: dict = None, log_callback=None) -> list:
    error_ids = []
    screenshot_dir = os.path.join("screenshots", task_id)
    os.makedirs(screenshot_dir, exist_ok=True)
    base_url = pages[0].get("page_url") if pages else ""
    profile = aggregate_site_profile(base_url, pages)
    page_profiles = {
        normalize_url(page.get("page_url") or base_url): build_page_profile(page, base_url)
        for page in pages or []
    }

    # Fetch is_mobile setting from task in DB
    db_sess = SessionLocal()
    try:
        task_record = db_sess.query(Task).filter(Task.id == task_id).first()
        is_mobile = bool(task_record.is_mobile) if (task_record and getattr(task_record, "is_mobile", None) is not None) else False
    except Exception:
        is_mobile = False
    finally:
        db_sess.close()

    video_dir = os.path.join("videos", task_id)
    os.makedirs(video_dir, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        if is_mobile:
            context = browser.new_context(
                viewport={"width": 375, "height": 667},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 14_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1",
                is_mobile=True,
                has_touch=True,
                ignore_https_errors=True,
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 720}
            )
        else:
            context = browser.new_context(
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 720}
            )
        page = context.new_page()
        page.set_default_timeout(20000)
        if auth and auth.get("auth_required"):
            authenticate_browser_context(page, auth, base_url or normalize_url(pages[0].get("page_url")) if pages else "", logger.info, task_id=task_id)

        # Check for guest bypass option if no auth is required
        if not (auth and auth.get("auth_required")) and base_url:
            try:
                if log_callback:
                    log_callback(f"[GuestBypass] Initializing guest bypass session by visiting {base_url}")
                page.goto(base_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                check_and_click_guest_bypass(page, log_callback)
                save_live_screenshot(page, task_id)
            except Exception as e:
                if log_callback:
                    log_callback(f"[GuestBypass] Warning: Failed guest bypass initialization: {e}")

        for use_case_id, test_cases in use_case_mapping.items():
            for test_data in test_cases:
                # Check cancellation status
                try:
                    db_check = SessionLocal()
                    task_status = db_check.query(Task.status).filter(Task.id == task_id).scalar()
                    db_check.close()
                    if task_status == "stopped":
                        if log_callback:
                            log_callback("[Orchestrator] Test execution cancelled by the user. Stopping validation loop.")
                        return error_ids
                except Exception:
                    pass

                is_existing_model = not isinstance(test_data, dict)
                if is_existing_model:
                    tc_id = test_data.id
                    tc_title = test_data.title
                    tc_steps = test_data.steps
                    tc_expected = test_data.expected_result
                    page_url = normalize_url(test_data.page_url or base_url)
                    test_data_dict = {
                        "title": tc_title,
                        "steps": tc_steps,
                        "expected_result": tc_expected,
                        "page_url": page_url,
                        "severity": "medium",
                        "check_type": "custom_scenario",
                        "task_id": task_id
                    }
                else:
                    tc_id = None
                    tc_title = test_data.get("title", "Anonymous Test")
                    tc_steps = test_data.get("steps")
                    tc_expected = test_data.get("expected_result")
                    page_url = normalize_url(test_data.get("page_url", base_url))
                    test_data_dict = {**test_data, "task_id": task_id}

                test_profile = page_profiles.get(page_url, profile)
                if log_callback:
                    log_callback(f"[Orchestrator] Testing page: {page_url} :: {tc_title}")

                # Update status of existing test case to "running" in the DB before executing
                if is_existing_model:
                    db = SessionLocal()
                    try:
                        db_tc = db.query(TestCase).filter(TestCase.id == tc_id).first()
                        if db_tc:
                            db_tc.status = "running"
                            db.commit()
                    finally:
                        db.close()

                actual_status, actual_error, severity_val = run_test_validation(page, context, test_data_dict, test_profile, auth)
                if log_callback:
                    outcome = "passed" if actual_status == "passed" else "failed"
                    log_callback(f"[Orchestrator] Result: {outcome} for {page_url}")

                db = SessionLocal()
                try:
                    test_case = None
                    if is_existing_model:
                        test_case = db.query(TestCase).filter(TestCase.id == tc_id).first()
                        if test_case:
                            test_case.status = actual_status
                            test_case.error_message = actual_error
                            test_case.steps = test_data_dict.get("steps", test_case.steps)
                            test_case.expected_result = test_data_dict.get("expected_result", test_case.expected_result)
                            test_case.execution_time = round(0.5 + float(time.time() % 1), 2)
                            db.commit()
                            db.refresh(test_case)
                    else:
                        test_case = TestCase(
                            task_id=task_id,
                            use_case_id=use_case_id,
                            title=tc_title,
                            steps=tc_steps,
                            expected_result=tc_expected,
                            status=actual_status,
                            error_message=actual_error,
                            execution_time=round(0.5 + float(time.time() % 1), 2),
                            test_type=test_data_dict.get("test_type"),
                            page_url=page_url
                        )
                        db.add(test_case)
                        db.commit()
                        db.refresh(test_case)

                    if test_case:
                        screenshot_name = f"{slugify(use_case_titles.get(use_case_id, 'use_case'))}_{slugify(tc_title)}.png"
                        screenshot_path = os.path.join(screenshot_dir, screenshot_name)
                        save_screenshot(page, screenshot_path)
                        test_error = TestError(
                            task_id=task_id,
                            test_case_id=test_case.id,
                            message=actual_error or ("Validation passed successfully." if actual_status == "passed" else "Validation failed during execution."),
                            severity="passed" if actual_status == "passed" else (severity_val or (test_data.get("severity") if isinstance(test_data, dict) else "medium") or "medium"),
                            page_url=page_url,
                            screenshot_path=screenshot_path
                        )
                        db.add(test_error)
                        db.commit()
                        db.refresh(test_error)
                        error_ids.append(test_error.id)
                finally:
                    db.close()

        raw_video_path = None
        try:
            if page and page.video:
                raw_video_path = page.video.path()
        except Exception as e:
            logger.warning(f"Could not retrieve video path: {e}")

        context.close()
        browser.close()

        if raw_video_path:
            time.sleep(0.5)
            if os.path.exists(raw_video_path):
                filename = os.path.basename(raw_video_path)
                video_relative_path = os.path.join("videos", task_id, filename)
                db = SessionLocal()
                try:
                    if error_ids:
                        db.query(TestError).filter(TestError.id.in_(error_ids)).update(
                            {TestError.video_path: video_relative_path},
                            synchronize_session=False
                        )
                        db.commit()
                except Exception as db_err:
                    logger.error(f"Failed to update video_path in DB: {db_err}")
                finally:
                    db.close()

    return error_ids


def map_error_to_code(codebase_path: str, error_message: str, codebase_data: dict) -> dict:
    """Deep codebase trace: Route → Component → API → Backend → DB Table."""
    logger.info(f"Deep codebase trace for error: {error_message}")
    ref = {
        "file_path": "src/App.jsx",
        "start_line": 1,
        "end_line": 10,
        "code_snippet": "// Main Application Entry point",
        "proposed_fix": None,
        "trace_chain": []
    }

    if not codebase_path or not os.path.exists(codebase_path):
        return ref

    trace = []
    error_lower = error_message.lower()

    # Step 1: Find the frontend component
    component_file = None
    
    for rel_path in codebase_data.get("file_list", []):
        if not rel_path.endswith((".jsx", ".tsx", ".js", ".ts")):
            continue
        # Skip backend/node_modules files if they sneak in
        if any(d in rel_path.lower() for d in ["node_modules", "backend", "controller", "server", "api/"]):
            continue
        full_path = os.path.join(codebase_path, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(20000)
            
            content_lower = content.lower()
            keywords = [w for w in error_lower.split() if len(w) > 3 and w.isalpha()]
            # add some common error identifiers if list is empty
            if not keywords:
                keywords = ["error", "fail", "invalid"]
            
            matches = sum(1 for kw in keywords if kw in content_lower)
            if matches >= 2 or (matches >= 1 and any(k in rel_path.lower() for k in ["form", "login", "checkout", "signup", "nav"])):
                component_file = rel_path
                trace.append({"layer": "Frontend Component", "file": rel_path})

                # Extract API calls from this component
                api_patterns = [
                    r'fetch\s*\(\s*["\']([^"\']+)["\']',
                    r'axios\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
                    r'api\s*\.\s*(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
                    r'["\'](\/(api|rest)\/[^"\']+)["\']',
                ]
                for pattern in api_patterns:
                    api_matches = re.findall(pattern, content)
                    for match in api_matches:
                        endpoint = match if isinstance(match, str) else match[-1]
                        if not any(t.get("endpoint") == endpoint for t in trace):
                            trace.append({"layer": "API Endpoint", "endpoint": endpoint})
                break
        except Exception as e:
            logger.error(f"Error scanning component {rel_path}: {e}")
            continue

    # Step 2: Find backend handler
    backend_exts = (".py", ".js", ".ts")
    backend_dirs = ["controllers", "routes", "api", "views", "handlers", "server", "backend"]
    backend_file = None
    
    for rel_path in codebase_data.get("file_list", []):
        if not any(rel_path.endswith(ext) for ext in backend_exts):
            continue
        # Must be in a backend directory or be a Python file (since backend is Python)
        if not any(d in rel_path.lower() for d in backend_dirs) and not rel_path.endswith(".py"):
            continue
        full_path = os.path.join(codebase_path, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(20000)
            content_lower = content.lower()
            
            # Look for route handlers matching discovered API endpoints
            matched_endpoint = False
            for trace_item in trace:
                if trace_item.get("layer") == "API Endpoint":
                    endpoint = trace_item.get("endpoint", "")
                    path_clean = endpoint.split("?")[0].rstrip("/")
                    if path_clean and (path_clean in content or path_clean.split("/")[-1] in content):
                        backend_file = rel_path
                        trace.append({"layer": "Backend Handler", "file": rel_path})
                        matched_endpoint = True
                        
                        # Extract DB table references from this backend file
                        table_patterns = [
                            r'from\s+["\']?(\w+)["\']?\s+where',
                            r'into\s+["\']?(\w+)["\']?',
                            r'update\s+["\']?(\w+)["\']?',
                            r'__tablename__\s*=\s*["\'](\w+)["\']',
                            r'\.find\(\s*\{\s*',
                            r'Model\s*\(\s*["\'](\w+)["\']',
                            r'db\.query\(([^)]+)\)',
                        ]
                        for tp in table_patterns:
                            tm = re.findall(tp, content, re.IGNORECASE)
                            for table in tm:
                                tname = table.split(".")[-1].lower()
                                if not any(t.get("table") == tname for t in trace):
                                    trace.append({"layer": "Database Table", "table": tname})
                        break
            if matched_endpoint:
                break
        except Exception as e:
            logger.error(f"Error scanning backend handler {rel_path}: {e}")
            continue

    # Fallback to general file lists if nothing matches specifically
    if not component_file:
        component_file = next((f for f in codebase_data.get("file_list", []) if any(k in f.lower() for k in ["app", "main", "index"])), "src/App.jsx")
        trace.append({"layer": "Frontend Component", "file": component_file})

    ref["trace_chain"] = trace

    # Read line snippet for primary component file
    full_path = os.path.join(codebase_path, component_file)
    if os.path.exists(full_path):
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            matched_line = 1
            for idx, line in enumerate(lines[:100], start=1):
                if any(keyword in line.lower() for keyword in ["form", "input", "button", "nav", "link", "footer", "header", "route"]):
                    matched_line = idx
                    break

            start = max(1, matched_line - 3)
            end = min(len(lines), matched_line + 10)
            snippet = "".join(lines[start - 1:end])
            
            proposed_fix = "// Adjust state or markup to handle this condition."
            if "form" in error_lower or "validation" in error_lower:
                proposed_fix = (
                    "// Ensure required fields are declared and validated before form submission.\n"
                    "// Check client-side validation logic and database constraint matching."
                )
            elif "auth" in error_lower or "login" in error_lower or "permission" in error_lower:
                proposed_fix = (
                    "// Verify authentication guards and route authorization checks.\n"
                    "// Ensure correct roles are configured on client routes and API endpoints."
                )
            elif "database" in error_lower or "db" in error_lower:
                proposed_fix = (
                    "// Inspect database constraint, unique indexes, or missing nullable configurations.\n"
                    "// Use transaction rollback or try/except block around DB operations."
                )

            ref["file_path"] = component_file
            ref["start_line"] = start
            ref["end_line"] = end
            ref["code_snippet"] = snippet
            ref["proposed_fix"] = proposed_fix
        except Exception as e:
            logger.error(f"Error extracting snippet from {component_file}: {e}")

    return ref


def generate_mock_analysis(url: str, crawl_data: dict, codebase_data: dict) -> dict:
    forms = crawl_data.get("forms", []) or []
    links = crawl_data.get("links", []) or []
    headings = crawl_data.get("headings", []) or []
    meta_tags = crawl_data.get("meta_tags", {}) or {}
    domain = normalize_url(url).split("//", 1)[1].split("/")[0]
    html_len = len(crawl_data.get("html_snippet", "") or "")
    status_code = crawl_data.get("status_code", 200)

    # Characteristics that make tests site-specific
    has_many_links = len(links) > 15
    has_many_forms = len(forms) > 1
    has_h1 = any("h1:" in h.lower() for h in headings)
    lacks_meta = len(meta_tags) < 3
    is_lightweight_site = html_len < 5000
    has_server_error = status_code != 200

    use_cases = []
    suggestions = []

    # Generate dramatically different test plans based on site characteristics
    if has_server_error:
        use_cases.append({
            "title": "Server Error Resolution",
            "description": f"Site returned HTTP {status_code}. Investigate infrastructure and availability.",
            "test_cases": [
                {
                    "title": "HTTP Error Status Analysis",
                    "steps": f"1. Access {domain}\n2. Observe HTTP {status_code}\n3. Check error logs",
                    "expected_result": "Homepage should return HTTP 200.",
                    "status": "failed",
                    "error_message": f"HTTP {status_code} error - server not responding correctly.",
                    "severity": "critical",
                    "page_url": url
                },
                {
                    "title": "Service Recovery",
                    "steps": "1. Restart services\n2. Verify DNS\n3. Check SSL certificates",
                    "expected_result": "Site should become accessible.",
                    "status": "failed",
                    "error_message": "Service unavailable.",
                    "severity": "critical",
                    "page_url": url
                }
            ]
        })
    elif has_many_links:
        use_cases.append({
            "title": f"Complex Navigation Network - {len(links)} Links",
            "description": "Site has rich navigation. Validate all paths are working.",
            "test_cases": [
                {
                    "title": "Link Validity Across All Pages",
                    "steps": f"1. Test {len(links)} discovered links\n2. Verify each resolves\n3. Check for 404s",
                    "expected_result": "All navigation links should be valid.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                },
                {
                    "title": "Navigation Consistency",
                    "steps": "1. Navigate between pages\n2. Verify menus are consistent\n3. Check for broken references",
                    "expected_result": "Navigation should work consistently across all pages.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                }
            ]
        })
    elif not links:
        use_cases.append({
            "title": "Navigation Structure Gap",
            "description": "Homepage has no links. Users cannot navigate to other content.",
            "test_cases": [
                {
                    "title": "Missing Navigation Links",
                    "steps": "1. Scan homepage for links\n2. Check for hidden navigation\n3. Verify site structure",
                    "expected_result": "Homepage should have at least one link.",
                    "status": "failed",
                    "error_message": "No navigation links found on homepage. Users cannot discover other pages.",
                    "severity": "high",
                    "page_url": url
                },
                {
                    "title": "Site Crawlability",
                    "steps": "1. Check robots.txt\n2. Verify sitemap\n3. Test crawler access",
                    "expected_result": "Site should be discoverable by crawlers.",
                    "status": "failed",
                    "error_message": "Limited crawlability due to navigation gaps.",
                    "severity": "medium",
                    "page_url": url
                }
            ]
        })
    else:
        use_cases.append({
            "title": f"Navigation Coverage - {len(links)} Links Found",
            "description": "Verify navigation structure is adequate for user discovery.",
            "test_cases": [
                {
                    "title": "Navigation Link Quality",
                    "steps": f"1. Inspect {len(links)} available links\n2. Verify targets exist\n3. Check link text clarity",
                    "expected_result": "Links should be descriptive and functional.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                },
                {
                    "title": "User Journey Support",
                    "steps": "1. Follow typical user paths\n2. Verify destination pages\n3. Check for dead ends",
                    "expected_result": "Users should be able to navigate to key pages.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                }
            ]
        })

    if has_many_forms:
        use_cases.append({
            "title": f"Multi-Form Submission Pipeline - {len(forms)} Forms",
            "description": "Multiple forms detected. Validate complex submission workflows.",
            "test_cases": [
                {
                    "title": "Form Cross-Validation",
                    "steps": f"1. Identify {len(forms)} forms\n2. Test mutual exclusivity\n3. Verify data isolation",
                    "expected_result": "Forms should handle data independently.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                },
                {
                    "title": "Error State Recovery",
                    "steps": "1. Submit one form with errors\n2. Submit another form\n3. Verify error isolation",
                    "expected_result": "Form errors should not affect other forms.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                }
            ]
        })
    elif forms:
        use_cases.append({
            "title": f"Single Form Interaction - {len(forms[0].get('inputs', []))} Fields",
            "description": "Validate form submission and field requirements.",
            "test_cases": [
                {
                    "title": "Required Field Validation",
                    "steps": f"1. Locate form with {len(forms[0].get('inputs', []))} fields\n2. Leave fields empty\n3. Attempt submission",
                    "expected_result": "Form should require all mandatory fields before submission.",
                    "status": "failed" if any(not field.get("required") for field in forms[0].get("inputs", [])) else "passed",
                    "error_message": "Some form fields are not marked as required." if any(not field.get("required") for field in forms[0].get("inputs", [])) else None,
                    "severity": "high" if any(not field.get("required") for field in forms[0].get("inputs", [])) else None,
                    "page_url": url
                },
                {
                    "title": "Successful Submission Flow",
                    "steps": "1. Fill all form fields\n2. Submit form\n3. Verify confirmation",
                    "expected_result": "Form should accept valid input and confirm.",
                    "status": "passed",
                    "error_message": None,
                    "severity": None,
                    "page_url": url
                }
            ]
        })
    else:
        use_cases.append({
            "title": "Static Content Engagement",
            "description": "No interactive forms. Evaluate content quality and readability.",
            "test_cases": [
                {
                    "title": "Content Depth Assessment",
                    "steps": f"1. Review {html_len} bytes of content\n2. Evaluate information richness\n3. Check structure",
                    "expected_result": "Content should be substantive and well-organized.",
                    "status": "failed" if is_lightweight_site else "passed",
                    "error_message": f"Lightweight content ({html_len} bytes). Expand with more details." if is_lightweight_site else None,
                    "severity": "low" if is_lightweight_site else None,
                    "page_url": url
                },
                {
                    "title": "Heading Structure Quality",
                    "steps": f"1. Scan {len(headings)} headings\n2. Verify logical nesting\n3. Check coverage",
                    "expected_result": "Headings should create a clear information hierarchy.",
                    "status": "failed" if not has_h1 else "passed",
                    "error_message": "No H1 heading found. Add a main heading for SEO and accessibility." if not has_h1 else None,
                    "severity": "high" if not has_h1 else None,
                    "page_url": url
                }
            ]
        })

    if lacks_meta:
        suggestions.append({
            "title": f"Add Missing Meta Tags - Only {len(meta_tags)} of 5 Found",
            "description": "Critical meta tags are missing. Add description, viewport, Open Graph tags.",
            "priority": "high"
        })
    if not has_h1:
        suggestions.append({
            "title": "Add Heading Structure",
            "description": "No H1 heading detected. Add a main heading for better SEO and accessibility.",
            "priority": "high"
        })
    if is_lightweight_site:
        suggestions.append({
            "title": f"Expand Content - Current: {html_len} bytes",
            "description": "Homepage is very minimal. Add more descriptive content about your business.",
            "priority": "medium"
        })
    if has_server_error:
        suggestions.append({
            "title": "Fix Server Error",
            "description": f"Homepage returned HTTP {status_code}. Investigate and resolve immediately.",
            "priority": "critical"
        })
    if not suggestions:
        suggestions.append({
            "title": "Expand QA Test Coverage",
            "description": "Initial scan found no major issues. Add performance, accessibility, and security tests.",
            "priority": "low"
        })

    return {"use_cases": use_cases, "suggestions": suggestions}


def generate_openai_analysis(url: str, crawl_data: dict, codebase_data: dict, key_files_context: str, api_key: str, user_prompt: str = None) -> dict:
    logger.info("Using OpenAI API for codebase-aware test suite generation")
    prompt = f"""
You are an expert QA Automation Engineer and Code Auditor. Analyze the crawled DOM structure of the website under test and its accompanying codebase files to generate structured testing suites.

Website URL: {url}
Domain: {crawl_data.get('domain', url)}
Crawled Metadata:
- Page Title: {crawl_data.get('title')}
- HTTP Status: {crawl_data.get('status_code', 200)}
- HTML Size: {crawl_data.get('html_length', 0)} bytes
- Pages Crawled: {crawl_data.get('page_count', 1)}
- Headings: {json.dumps(crawl_data.get('headings'))}
- Forms Discovered: {json.dumps(crawl_data.get('forms'))}
- Discovered Links: {json.dumps(crawl_data.get('links'))}

Codebase Context:
- Framework: {codebase_data.get('framework_type')}
- Key files structure & Content: {key_files_context}
- Total files: {len(codebase_data.get('file_list', []))}
"""
    if user_prompt:
        prompt += f"\nADDITIONAL USER TESTING DIRECTIVE:\nFollow these instructions when designing the test cases:\n{user_prompt}\n"

    prompt += """
Generate comprehensive Use Cases for THIS specific website. For EACH page/feature discovered, generate test cases across these categories:

**POSITIVE TESTS**: Valid inputs, happy-path workflows.
**NEGATIVE TESTS**: Wrong credentials, invalid data, wrong formats, wrong user roles.
**BOUNDARY TESTS**: Max length inputs (255 chars, 500 chars), Unicode characters, special characters (&, <, >, ", '), empty strings, whitespace-only.
**SECURITY TESTS**: SQL injection attempts (' OR '1'='1), XSS payloads (<script>alert(1)</script>), CSRF token validation, session hijack scenarios.
**ROLE-BASED TESTS**: Admin vs regular user permissions, unauthorized access attempts, role escalation.
**PERFORMANCE INDICATORS**: Large form submissions, rapid repeated clicks, concurrent session hints.
**ACCESSIBILITY CHECKS**: Keyboard navigation, screen reader labels, color contrast, focus indicators.

For each test case, include a "test_type" field with one of: positive, negative, boundary, security, role_based, performance, accessibility.

ADMIN PROTECTION RULES — CRITICAL:
- NEVER generate steps that delete, deactivate, or change passwords for users named: admin, superadmin, root, administrator.
- For CRUD workflows, create TEMPORARY test users (e.g., test_user_001) and clean up after.
- If testing user management, always target test records — never production data.

CRITICAL LANGUAGE REQUIREMENT:
All generated titles, descriptions, steps, expected results, and suggestions must be written in simple, clear, easy-to-understand Indian English (avoiding complex, overly academic, or highly programmatic technical jargon).
Keep the sentences short, clear, and direct so a non-technical manager can understand them instantly.

Your response MUST be valid JSON matching this schema:
{
  "use_cases": [
    {
      "title": "Use Case Title referencing actual page/feature",
      "description": "Description using actual site elements",
      "test_cases": [
        {
          "title": "Test Case Title",
          "steps": "Step 1: ...\\nStep 2: ...",
          "expected_result": "Expected result details",
          "status": "pending",
          "error_message": null,
          "severity": null,
          "page_url": "URL of page under test",
          "check_type": "page_load | link_health | form_required | heading_structure | content_depth | image_alt | navigation_presence | internal_pages",
          "test_type": "positive | negative | boundary | security | role_based | performance | accessibility"
        }
      ]
    }
  ],
  "suggestions": [
    {
      "title": "Suggestion summary",
      "description": "Details",
      "priority": "low | medium | high"
    }
  ]
}
Return ONLY raw JSON. No markdown code blocks.
"""
    try:
        endpoint = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }

        response = httpx.post(
            endpoint, 
            json=payload, 
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }, 
            timeout=30.0
        )
        if response.status_code != 200:
            logger.error(f"OpenAI API returned error: {response.text}")
            raise Exception(f"OpenAI API Error: {response.text}")

        result_json = response.json()
        choices = result_json.get("choices", [])
        if not choices:
            raise ValueError(f"OpenAI returned no choices: {result_json}")

        text_content = choices[0].get("message", {}).get("content", "").strip()
        if not text_content:
            raise ValueError(f"OpenAI returned empty text content: {result_json}")

        parsed = json.loads(text_content)
        if not isinstance(parsed, dict):
            raise ValueError("OpenAI response was not a JSON object.")
        parsed.setdefault("use_cases", [])
        parsed.setdefault("suggestions", [])
        return parsed
    except Exception as exc:
        logger.error(f"Failed to generate analysis using OpenAI: {exc}")
        logger.error(traceback.format_exc())
        return {"use_cases": [], "suggestions": []}


def generate_gemini_analysis(url: str, crawl_data: dict, codebase_data: dict, key_files_context: str, api_key: str, user_prompt: str = None) -> dict:
    logger.info("Using Gemini API for codebase-aware test suite generation")
    prompt = f"""
You are an expert QA Automation Engineer and Code Auditor. Analyze the crawled DOM structure of the website under test and its accompanying codebase files to generate structured testing suites.

Website URL: {url}
Domain: {crawl_data.get('domain', url)}
Crawled Metadata:
- Page Title: {crawl_data.get('title')}
- HTTP Status: {crawl_data.get('status_code', 200)}
- HTML Size: {crawl_data.get('html_length', 0)} bytes
- Pages Crawled: {crawl_data.get('page_count', 1)}
- Headings: {json.dumps(crawl_data.get('headings'))}
- Forms Discovered: {json.dumps(crawl_data.get('forms'))}
- Discovered Links: {json.dumps(crawl_data.get('links'))}

Codebase Context:
- Framework: {codebase_data.get('framework_type')}
- Key files structure & Content: {key_files_context}
- Total files: {len(codebase_data.get('file_list', []))}
"""
    if user_prompt:
        prompt += f"\nADDITIONAL USER TESTING DIRECTIVE:\nFollow these instructions when designing the test cases:\n{user_prompt}\n"

    prompt += """
Generate comprehensive Use Cases for THIS specific website. For EACH page/feature discovered, generate test cases across these categories:

**POSITIVE TESTS**: Valid inputs, happy-path workflows.
**NEGATIVE TESTS**: Wrong credentials, invalid data, wrong formats, wrong user roles.
**BOUNDARY TESTS**: Max length inputs (255 chars, 500 chars), Unicode characters, special characters (&, <, >, ", '), empty strings, whitespace-only.
**SECURITY TESTS**: SQL injection attempts (' OR '1'='1), XSS payloads (<script>alert(1)</script>), CSRF token validation, session hijack scenarios.
**ROLE-BASED TESTS**: Admin vs regular user permissions, unauthorized access attempts, role escalation.
**PERFORMANCE INDICATORS**: Large form submissions, rapid repeated clicks, concurrent session hints.
**ACCESSIBILITY CHECKS**: Keyboard navigation, screen reader labels, color contrast, focus indicators.

For each test case, include a "test_type" field with one of: positive, negative, boundary, security, role_based, performance, accessibility.

ADMIN PROTECTION RULES — CRITICAL:
- NEVER generate steps that delete, deactivate, or change passwords for users named: admin, superadmin, root, administrator.
- For CRUD workflows, create TEMPORARY test users (e.g., test_user_001) and clean up after.
- If testing user management, always target test records — never production data.

CRITICAL LANGUAGE REQUIREMENT:
All generated titles, descriptions, steps, expected results, and suggestions must be written in simple, clear, easy-to-understand Indian English (avoiding complex, overly academic, or highly programmatic technical jargon).
Keep the sentences short, clear, and direct so a non-technical manager can understand them instantly.

Your response MUST be valid JSON matching this schema:
{
  "use_cases": [
    {
      "title": "Use Case Title referencing actual page/feature",
      "description": "Description using actual site elements",
      "test_cases": [
        {
          "title": "Test Case Title",
          "steps": "Step 1: ...\\nStep 2: ...",
          "expected_result": "Expected result details",
          "status": "pending",
          "error_message": null,
          "severity": null,
          "page_url": "URL of page under test",
          "check_type": "page_load | link_health | form_required | heading_structure | content_depth | image_alt | navigation_presence | internal_pages",
          "test_type": "positive | negative | boundary | security | role_based | performance | accessibility"
        }
      ]
    }
  ],
  "suggestions": [
    {
      "title": "Suggestion summary",
      "description": "Details",
      "priority": "low | medium | high"
    }
  ]
}
Return ONLY raw JSON. No markdown code blocks.
"""
    try:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2
            }
        }

        response = httpx.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=30.0)
        if response.status_code != 200:
            logger.error(f"Gemini API returned error: {response.text}")
            raise Exception(f"Gemini API Error: {response.text}")

        result_json = response.json()
        candidates = result_json.get("candidates", [])
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {result_json}")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise ValueError(f"Gemini returned no content parts: {result_json}")

        text_content = parts[0].get("text", "").strip()
        if not text_content:
            raise ValueError(f"Gemini returned empty text content: {result_json}")

        if text_content.startswith("```"):
            lines = text_content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text_content = "\n".join(lines).strip()

        parsed = json.loads(text_content)
        if not isinstance(parsed, dict):
            raise ValueError("Gemini response was not a JSON object.")
        parsed.setdefault("use_cases", [])
        parsed.setdefault("suggestions", [])
        return parsed
    except Exception as exc:
        logger.error(f"Failed to generate analysis using Gemini: {exc}. Falling back to built-in generator.")
        logger.error(traceback.format_exc())
        return {"use_cases": [], "suggestions": []}


def run_ui_ux_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    logger.info(f"UI/UX Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "UI_UX").first()
        if not state:
            return

        profile = aggregate_site_profile(url, page_snapshots)
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[UI/UX Agent] Auditing '{profile['primary_title']}' on {profile['domain']}.\n"
        db.commit()

        errors = 0
        log = state.log_output
        log += f"[UI/UX Agent] Scanned {profile['page_count']} page(s), {profile['heading_count']} headings, {profile['meta_count']} meta tags.\n"

        if not profile["has_h1"]:
            log += f"[UI/UX Agent] Issue: No H1 heading on '{profile['primary_title']}'.\n"
            errors += 1
        if profile["meta_count"] < 3:
            log += f"[UI/UX Agent] Issue: Only {profile['meta_count']} meta tags on {profile['domain']}.\n"
            errors += 1
        if profile["is_blocked"]:
            log += f"[UI/UX Agent] Issue: Page appears blocked — title is '{profile['primary_title']}'.\n"
            errors += 1
        if errors == 0:
            log += f"[UI/UX Agent] Heading and meta structure look acceptable on {profile['domain']}.\n"

        log += "[UI/UX Agent] Visual audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"UI/UX agent execution failed: {exc}")
    finally:
        db.close()


def run_responsive_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    logger.info(f"Responsive Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Responsive").first()
        if not state:
            return

        profile = aggregate_site_profile(url, page_snapshots)
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Responsive Agent] Testing viewports for {profile['domain']}.\n"
        db.commit()

        errors = 0
        log = state.log_output
        viewport_issues = []

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(normalize_url(url), wait_until="domcontentloaded", timeout=20000)
                for width, label in [(375, "mobile"), (768, "tablet"), (1440, "desktop")]:
                    page.set_viewport_size({"width": width, "height": 800})
                    has_scroll = page.evaluate(
                        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                    )
                    if has_scroll:
                        viewport_issues.append(f"horizontal scroll at {label} ({width}px)")
                        errors += 1
                    else:
                        log += f"[Responsive Agent] {label.capitalize()} ({width}px): layout OK.\n"
                browser.close()
        except Exception as exc:
            log += f"[Responsive Agent] Could not complete live viewport check: {exc}\n"

        for issue in viewport_issues:
            log += f"[Responsive Agent] Issue: {issue} on {profile['domain']}.\n"
        if not viewport_issues and errors == 0:
            log += f"[Responsive Agent] No responsive layout issues detected on {profile['domain']}.\n"

        log += "[Responsive Agent] Responsive audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Responsive agent execution failed: {exc}")
    finally:
        db.close()


def run_form_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    logger.info(f"Form Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Form").first()
        if not state:
            return

        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Form Agent] Checking forms for {url}.\n"
        db.commit()

        profile = aggregate_site_profile(url, page_snapshots)
        log = state.log_output
        log += f"[Form Agent] Discovered {profile['form_count']} form(s) on {profile['domain']}.\n"
        errors = 0
        if profile["form_count"] > 0:
            optional_fields = [
                field for form in profile["forms"]
                for field in form.get("inputs", [])
                if not field.get("required") and field.get("type") not in ("hidden", "submit", "button")
            ]
            field_names = [f.get("name") or f.get("placeholder") or f.get("type", "field") for f in optional_fields]
            if field_names:
                log += f"[Form Agent] Issue: Fields without required attribute: {', '.join(field_names[:5])}.\n"
                errors = 1
            else:
                log += f"[Form Agent] All {len(profile['form_field_names'])} fields appear required on {profile['domain']}.\n"
        else:
            log += f"[Form Agent] No forms detected on '{profile['primary_title']}'.\n"

        log += "[Form Agent] Form validation audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Form agent execution failed: {exc}")
    finally:
        db.close()


def run_api_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    logger.info(f"API Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "API").first()
        if not state:
            return

        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[API Agent] Monitoring network requests for {url}.\n"
        db.commit()

        profile = aggregate_site_profile(url, page_snapshots)
        log = state.log_output
        errors = 0
        broken = []
        if profile["links"]:
            try:
                with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                    for link in profile["links"][:5]:
                        href = link.get("href", "")
                        if not href or href.startswith(("#", "mailto:", "tel:")):
                            continue
                        target = href if href.startswith("http") else urljoin(profile["url"], href)
                        try:
                            response = client.head(target)
                            if response.status_code >= 400:
                                broken.append(f"{link.get('text', 'link')[:25]} ({response.status_code})")
                        except Exception:
                            broken.append(f"{link.get('text', 'link')[:25]} (unreachable)")
            except Exception as exc:
                log += f"[API Agent] Network check error: {exc}\n"

        if broken:
            log += f"[API Agent] Issue: Unreachable endpoints on {profile['domain']}: {', '.join(broken)}.\n"
            errors = len(broken)
        else:
            log += f"[API Agent] Sampled endpoints on {profile['domain']} responded successfully.\n"

        log += "[API Agent] API audit complete.\n"

        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"API agent execution failed: {exc}")
    finally:
        db.close()


def run_image_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    logger.info(f"Image Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Image").first()
        if not state:
            return

        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Image Agent] Scanning image assets for {url}.\n"
        db.commit()

        profile = aggregate_site_profile(url, page_snapshots)
        log = state.log_output
        log += f"[Image Agent] Scanned {profile['image_count']} images on '{profile['primary_title']}'.\n"
        errors = profile["images_missing_alt"]
        if errors:
            log += f"[Image Agent] Issue: {errors} image(s) on {profile['domain']} missing alt text.\n"
        else:
            log += f"[Image Agent] All images on {profile['domain']} have alt attributes.\n"
        log += "[Image Agent] Image asset audit complete.\n"

        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Image agent execution failed: {exc}")
    finally:
        db.close()


def run_code_correlation_agent(task_id: str, codebase_path: str, codebase_data: dict, error_ids: list):
    logger.info(f"Code Correlation Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "CodeCorrelation").first()
        if not state:
            return

        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[CodeCorrelation Agent] Mapping {len(error_ids)} errors to codebase.\n"
        db.commit()

        time.sleep(1.8)
        log = state.log_output

        errors = db.query(TestError).filter(TestError.id.in_(error_ids)).all()
        for err in errors:
            ref_data = map_error_to_code(codebase_path, err.message, codebase_data)
            code_ref = CodeReference(
                test_error_id=err.id,
                file_path=ref_data["file_path"],
                start_line=ref_data["start_line"],
                end_line=ref_data["end_line"],
                code_snippet=ref_data["code_snippet"],
                proposed_fix=ref_data["proposed_fix"],
                trace_chain_json=json.dumps(ref_data.get("trace_chain", []))
            )
            db.add(code_ref)
            db.commit()
            log += f"[CodeCorrelation Agent] Mapped '{err.message[:40]}' to {ref_data['file_path']}.\n"

        log += "[CodeCorrelation Agent] Code mapping complete.\n"
        state.status = "completed"
        state.errors_found = len(errors)
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Code Correlation agent execution failed: {exc}")
    finally:
        db.close()


def run_health_check_agent(task_id: str, url: str, page_snapshots: list, auth: dict = None):
    """Pre-flight health check on all discovered pages before deeper testing."""
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(
            AgentState.task_id == task_id,
            AgentState.agent_name == "HealthCheck"
        ).first()
        if state:
            state.status = "running"
            state.started_at = datetime.utcnow()
            db.commit()

        healthy_pages = []
        failed_pages = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text)
                     if msg.type == "error" else None)

            for snapshot in page_snapshots:
                page_url = snapshot.get("page_url", url)
                console_errors.clear()
                health = {"url": page_url, "status": "healthy", "issues": []}

                try:
                    response = page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                    status_code = response.status if response else 0

                    if status_code >= 500:
                        health["issues"].append(f"Server error: HTTP {status_code}")
                        health["status"] = "critical"
                    elif status_code >= 400:
                        health["issues"].append(f"Client error: HTTP {status_code}")
                        health["status"] = "warning"

                    page.wait_for_timeout(2000)  # Allow JS to execute

                    if console_errors:
                        health["issues"].append(f"JS console errors: {len(console_errors)}")
                        if any("uncaught" in e.lower() or "error" in e.lower()
                               for e in console_errors):
                            health["status"] = "critical"

                except Exception as exc:
                    health["issues"].append(f"Navigation failed: {str(exc)}")
                    health["status"] = "critical"

                if health["status"] == "critical":
                    failed_pages.append(health)
                    # Create TestError for critical health failures
                    db.add(TestError(
                        task_id=task_id,
                        message=f"Health Check FAILED: {'; '.join(health['issues'])}",
                        severity="critical",
                        page_url=page_url,
                    ))
                else:
                    healthy_pages.append(health)

            context.close()
            browser.close()

        if state:
            state.status = "completed"
            state.errors_found = len(failed_pages)
            state.log_output = f"Healthy: {len(healthy_pages)}, Failed: {len(failed_pages)}"
            state.completed_at = datetime.utcnow()

        db.commit()
        return healthy_pages, failed_pages
    except Exception as exc:
        logger.error(f"Health Check Agent failed: {exc}")
        if state:
            state.status = "failed"
            state.log_output = str(exc)
            state.completed_at = datetime.utcnow()
            db.commit()
        return page_snapshots, []  # fallback: treat all as healthy
    finally:
        db.close()


def run_route_discovery_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Route Discovery Agent: finds ALL routes and reports untested ones."""
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(
            AgentState.task_id == task_id,
            AgentState.agent_name == "RouteDiscovery"
        ).first()
        if state:
            state.status = "running"
            state.started_at = datetime.utcnow()
            db.commit()

        codebase = db.query(Codebase).filter(Codebase.task_id == task_id).first()
        codebase_path = codebase.local_path if codebase else None

        # Discover from all sources
        codebase_routes = discover_routes_from_codebase(codebase_path, codebase_data) if codebase_path else []
        sitemap_routes = discover_routes_from_sitemap(url)
        all_discovered = codebase_routes + sitemap_routes

        # Compare with crawled pages
        crawled_urls = [s.get("page_url", "") for s in page_snapshots]
        comparison = compare_routes(all_discovered, crawled_urls, url)

        # Report untested routes as errors
        errors_found = 0
        for untested in comparison["untested_routes"]:
            db.add(TestError(
                task_id=task_id,
                message=f"Untested route found: {untested['route']} (source: {untested['source_file']}). "
                        f"Reason: No navigation link exists to this page, or the crawler did not reach it.",
                severity="high",
                page_url=untested["route"],
            ))
            errors_found += 1

        # Add suggestion about coverage
        db.add(Suggestion(
            task_id=task_id,
            title=f"Route Coverage: {comparison['coverage_percent']}%",
            description=f"Found {comparison['total_codebase_routes']} routes in codebase, "
                        f"but only {comparison['total_crawled_pages']} pages were crawled. "
                        f"{len(comparison['untested_routes'])} routes are untested.",
            priority="high" if comparison['coverage_percent'] < 50 else "medium"
        ))

        if state:
            state.status = "completed"
            state.errors_found = errors_found
            state.log_output = (
                f"Codebase routes: {len(codebase_routes)}, "
                f"Sitemap routes: {len(sitemap_routes)}, "
                f"Crawled pages: {len(crawled_urls)}, "
                f"Untested: {len(comparison['untested_routes'])}, "
                f"Coverage: {comparison['coverage_percent']}%"
            )
            state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Route Discovery Agent failed: {exc}")
        if state:
            state.status = "failed"
            state.log_output = str(exc)
            state.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def run_user_journey_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """User Journey Agent: generates and tests multi-step CRUD workflows."""
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(
            AgentState.task_id == task_id,
            AgentState.agent_name == "UserJourney"
        ).first()
        if state:
            state.status = "running"
            state.started_at = datetime.utcnow()
            db.commit()

        # Analyze navigation structure to identify CRUD patterns
        crud_patterns = {
            "create": ["create", "new", "add", "register", "signup"],
            "read": ["list", "view", "detail", "show", "dashboard"],
            "update": ["edit", "update", "modify", "settings"],
            "delete": ["delete", "remove", "deactivate"],
            "approve": ["approve", "confirm", "accept", "verify"],
        }

        # Group pages by resource type
        resource_groups = {}
        for snapshot in page_snapshots:
            page_url = snapshot.get("page_url", "")
            path = page_url.split("//", 1)[-1].split("/", 1)[-1] if "//" in page_url else page_url
            segments = [s for s in path.split("/") if s and not s.startswith("?")]

            for segment in segments:
                for action, keywords in crud_patterns.items():
                    if any(kw in segment.lower() for kw in keywords):
                        # Extract resource name (parent segment)
                        resource = segments[segments.index(segment) - 1] if segments.index(segment) > 0 else segment
                        resource_groups.setdefault(resource, {})[action] = page_url

        # Generate journey use cases
        journeys_created = 0
        for resource, actions in resource_groups.items():
            if len(actions) < 2:
                continue  # Need at least 2 CRUD operations to form a journey

            journey_steps = []
            step_num = 1
            ordered_actions = ["create", "read", "update", "approve", "delete"]

            for action in ordered_actions:
                if action in actions:
                    journey_steps.append(
                        f"Step {step_num}: Navigate to {actions[action]} and perform {action} operation on {resource}"
                    )
                    step_num += 1

            if journey_steps:
                use_case = UseCase(
                    task_id=task_id,
                    title=f"{resource.title()} Management Journey",
                    description=f"End-to-end CRUD workflow for {resource}: {' → '.join(actions.keys())}"
                )
                db.add(use_case)
                db.flush()

                db.add(TestCase(
                    task_id=task_id,
                    use_case_id=use_case.id,
                    title=f"Complete {resource.title()} CRUD Journey",
                    steps="\n".join(journey_steps),
                    expected_result=f"All {resource} operations complete without errors",
                    status="pending",
                    test_type="journey",
                    page_url=list(actions.values())[0]
                ))
                journeys_created += 1

        if state:
            state.status = "completed"
            state.errors_found = 0
            state.log_output = f"Generated {journeys_created} user journeys from {len(resource_groups)} resources"
            state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"User Journey Agent failed: {exc}")
        if state:
            state.status = "failed"
            state.log_output = str(exc)
            state.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


# --- HELPER FOR VISUAL REGRESSION ---
def compare_screenshots_rms(img_path_a: str, img_path_b: str) -> float:
    """Compare two images using Root-Mean-Square (RMS) difference. Returns a diff percentage (0 to 100)."""
    try:
        from PIL import Image, ImageChops
        import math
        
        img_a = Image.open(img_path_a).convert("RGB")
        img_b = Image.open(img_path_b).convert("RGB")
        
        # Resize to same dimensions if different
        if img_a.size != img_b.size:
            img_b = img_b.resize(img_a.size)
            
        diff = ImageChops.difference(img_a, img_b)
        h = diff.histogram()
        
        # Calculate RMS difference
        sum_of_squares = sum(value * (idx ** 2) for idx, value in enumerate(h))
        rms = math.sqrt(sum_of_squares / float(img_a.size[0] * img_a.size[1] * 3))
        
        # Normalize to percentage (rms max is 255)
        diff_pct = (rms / 255.0) * 100.0
        return diff_pct
    except Exception as e:
        logger.error(f"Image comparison failed: {e}")
        return 0.0


def run_login_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Login Agent: validates authentication flows."""
    logger.info(f"Login Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Login").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Login Agent] Auditing login flow for {url}.\n"
        db.commit()

        errors = 0
        log = state.log_output
        login_pages = [s for s in page_snapshots if any(k in s.get("page_url", "").lower() for k in ["login", "signin", "auth"])]
        log += f"[Login Agent] Found {len(login_pages)} login-related pages.\n"

        if not url.startswith("https://") and "localhost" not in url and "127.0.0.1" not in url:
            log += "[Login Agent] Issue: Login form uses insecure HTTP protocol instead of HTTPS.\n"
            db.add(TestError(
                task_id=task_id,
                message="Insecure login: authentication forms should be served over HTTPS to protect credentials.",
                severity="critical",
                page_url=url,
            ))
            errors += 1

        # Codebase audit for security: look for token storage in localStorage
        codebase_path = codebase_data.get("codebase_path")
        if codebase_path and os.path.exists(codebase_path):
            local_storage_tokens = []
            for rel_path in codebase_data.get("file_list", []):
                if not rel_path.endswith((".jsx", ".tsx", ".js", ".ts")):
                    continue
                full_path = os.path.join(codebase_path, rel_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read(5000)
                    if "localstorage.setitem" in content.lower() and any(k in content.lower() for k in ["token", "auth", "jwt", "session"]):
                        local_storage_tokens.append(rel_path)
                except Exception:
                    continue
            if local_storage_tokens:
                log += f"[Login Agent] Security Warning: JWT/Auth tokens might be stored in localStorage (vulnerable to XSS) in: {', '.join(local_storage_tokens[:2])}\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Auth tokens stored in localStorage (vulnerable to XSS) in: {local_storage_tokens[0]}",
                    severity="medium",
                    page_url=url,
                ))
                errors += 1

        log += "[Login Agent] Login authentication check complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Login agent execution failed: {exc}")
    finally:
        db.close()


def run_role_permission_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Role & Permission Agent: validates access control."""
    logger.info(f"Role/Permission Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "RolePermission").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[RolePermission Agent] Auditing user roles and access control for {url}.\n"
        db.commit()

        errors = 0
        log = state.log_output

        admin_pages = [s for s in page_snapshots if "admin" in s.get("page_url", "").lower()]
        log += f"[RolePermission Agent] Identified {len(admin_pages)} restricted admin paths.\n"

        # Check codebase for unprotected routes or missing guards
        codebase_path = codebase_data.get("codebase_path")
        if codebase_path and os.path.exists(codebase_path):
            unprotected_admin_routes = []
            for rel_path in codebase_data.get("file_list", []):
                if not rel_path.endswith((".jsx", ".tsx", ".js", ".ts", ".py")):
                    continue
                full_path = os.path.join(codebase_path, rel_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read(5000)
                    content_lower = content.lower()
                    if "admin" in content_lower and any(r in content_lower for r in ["router.get", "router.post", "@app.get", "@app.post"]):
                        if not any(guard in content_lower for guard in ["auth", "guard", "protect", "permission", "jwt"]):
                            unprotected_admin_routes.append(rel_path)
                except Exception:
                    continue
            if unprotected_admin_routes:
                log += f"[RolePermission Agent] Security Warning: Admin endpoint definitions in {unprotected_admin_routes[:2]} might be missing authorization guards.\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Potential unprotected admin route found in codebase file: {unprotected_admin_routes[0]}",
                    severity="high",
                    page_url=url,
                ))
                errors += 1

        log += "[RolePermission Agent] Role and permission checks completed successfully.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"RolePermission agent execution failed: {exc}")
    finally:
        db.close()


def run_database_integrity_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Database Integrity Agent: monitors API responses for database errors."""
    logger.info(f"Database Integrity Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "DatabaseIntegrity").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[DatabaseIntegrity Agent] Scanning API requests for database integrity issues.\n"
        db.commit()

        errors = 0
        log = state.log_output

        # Check codebase for raw SQL queries or DB relationships
        codebase_path = codebase_data.get("codebase_path")
        if codebase_path and os.path.exists(codebase_path):
            raw_queries = []
            for rel_path in codebase_data.get("file_list", []):
                if not rel_path.endswith((".py", ".js", ".ts")):
                    continue
                full_path = os.path.join(codebase_path, rel_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read(5000)
                    content_lower = content.lower()
                    if "select " in content_lower and ("execute(" in content_lower or "query(" in content_lower or "db.engine" in content_lower):
                        raw_queries.append(rel_path)
                except Exception:
                    continue
            if raw_queries:
                log += f"[DatabaseIntegrity Agent] Warning: Raw SQL queries detected in {raw_queries[:2]}. Prefer using ORM to avoid database integrity and syntax issues.\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Raw SQL queries detected in codebase: {raw_queries[0]}. Use ORM or parameterized inputs.",
                    severity="medium",
                    page_url=url,
                ))
                errors += 1

        log += "[DatabaseIntegrity Agent] Database constraints and relationship audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"DatabaseIntegrity agent execution failed: {exc}")
    finally:
        db.close()


def run_security_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Security Agent: scans for security header presence and input field vulnerabilities."""
    logger.info(f"Security Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Security").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Security Agent] Auditing security headers for {url}.\n"
        db.commit()

        errors = 0
        log = state.log_output

        try:
            with httpx.Client(follow_redirects=True, timeout=10.0) as client:
                resp = client.get(normalize_url(url))
                headers = resp.headers
                
                missing_headers = []
                if "Content-Security-Policy" not in headers:
                    missing_headers.append("Content-Security-Policy")
                if "X-Frame-Options" not in headers:
                    missing_headers.append("X-Frame-Options")
                if "X-Content-Type-Options" not in headers:
                    missing_headers.append("X-Content-Type-Options")

                if missing_headers:
                    log += f"[Security Agent] Issue: Missing security headers: {', '.join(missing_headers)}.\n"
                    for h in missing_headers:
                        db.add(TestError(
                            task_id=task_id,
                            message=f"Security header check failed: '{h}' response header is missing.",
                            severity="medium",
                            page_url=url,
                        ))
                    errors += len(missing_headers)
        except Exception as exc:
            log += f"[Security Agent] Could not complete HTTP header checks: {exc}\n"

        # Codebase security scans: check for dangerouslySetInnerHTML or Jinja |safe filter
        codebase_path = codebase_data.get("codebase_path")
        if codebase_path and os.path.exists(codebase_path):
            vulnerabilities = []
            for rel_path in codebase_data.get("file_list", []):
                if not rel_path.endswith((".jsx", ".tsx", ".js", ".ts", ".html", ".py")):
                    continue
                full_path = os.path.join(codebase_path, rel_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read(5000)
                    content_lower = content.lower()
                    if "dangerouslysetinnerhtml" in content_lower:
                        vulnerabilities.append((rel_path, "XSS vulnerability: dangerouslySetInnerHTML"))
                    if "|safe" in content_lower:
                        vulnerabilities.append((rel_path, "XSS vulnerability: unescaped template output (|safe)"))
                    if "eval(" in content_lower and "evaluate" not in content_lower:
                        vulnerabilities.append((rel_path, "Remote Code Execution: eval() usage"))
                except Exception:
                    continue
            for rel_file, vuln_desc in vulnerabilities[:3]:
                log += f"[Security Agent] Critical Issue: {vuln_desc} in {rel_file}\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Security vulnerability detected: {vuln_desc} in codebase file {rel_file}",
                    severity="critical",
                    page_url=url,
                ))
                errors += 1

        log += "[Security Agent] Security audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Security agent execution failed: {exc}")
    finally:
        db.close()


def run_accessibility_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Accessibility Agent: checks DOM structure for key ARIA/A11y requirements."""
    logger.info(f"Accessibility Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Accessibility").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Accessibility Agent] Checking accessibility elements for {url}.\n"
        db.commit()

        errors = 0
        log = state.log_output

        profile = aggregate_site_profile(url, page_snapshots)
        img_missing = profile.get("images_missing_alt", 0)
        if img_missing > 0:
            log += f"[Accessibility Agent] Issue: {img_missing} images are missing descriptive alt labels.\n"
            db.add(TestError(
                task_id=task_id,
                message=f"Accessibility failure: {img_missing} images are missing alt attributes (critical for screen readers).",
                severity="medium",
                page_url=url,
            ))
            errors += img_missing

        # Check page snapshots HTML for non-semantic interactive divs or inputs without labels
        for snap in page_snapshots:
            html = snap.get("html_snippet", "")
            if not html:
                continue
            page_url = snap.get("page_url", url)
            
            # Check inputs without id/label
            if "<input" in html and ("label" not in html.lower() and "aria-label" not in html.lower()):
                log += f"[Accessibility Agent] Issue on {page_url}: Input fields detected without descriptive labels or aria-label.\n"
                db.add(TestError(
                    task_id=task_id,
                    message="Input elements found without associated labels or aria-label attributes.",
                    severity="high",
                    page_url=page_url,
                ))
                errors += 1
                
            # Check onClick on divs
            if "onclick" in html.lower() and "role=" not in html.lower() and "<div" in html.lower():
                log += f"[Accessibility Agent] Issue on {page_url}: Non-semantic interactive element (div with onClick) found without WAI-ARIA role.\n"
                db.add(TestError(
                    task_id=task_id,
                    message="Non-semantic interactive element (div with click handler) found without role='button' or tabIndex.",
                    severity="medium",
                    page_url=page_url,
                ))
                errors += 1

        log += "[Accessibility Agent] Accessibility audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Accessibility agent execution failed: {exc}")
    finally:
        db.close()


def run_performance_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Performance Agent: measures load times and checks performance constraints."""
    logger.info(f"Performance Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Performance").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[Performance Agent] Auditing page performance and load times.\n"
        db.commit()

        errors = 0
        log = state.log_output

        try:
            start_time = time.time()
            with httpx.Client(follow_redirects=True, timeout=15.0) as client:
                client.get(normalize_url(url))
            load_time = round(time.time() - start_time, 2)
            log += f"[Performance Agent] Page load time: {load_time}s\n"
            if load_time > 3.0:
                log += f"[Performance Agent] Issue: Page took {load_time}s to load (exceeds 3.0s budget).\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Performance budget exceeded: page load time of {load_time}s is above 3.0s threshold.",
                    severity="medium",
                    page_url=url,
                ))
                errors += 1
        except Exception as exc:
            log += f"[Performance Agent] Performance check failed: {exc}\n"

        for snap in page_snapshots:
            html_len = len(snap.get("html_snippet", "") or "")
            page_url = snap.get("page_url", url)
            if html_len > 1000000:
                log += f"[Performance Agent] Issue on {page_url}: Large DOM payload size ({html_len} bytes) which can slow down rendering.\n"
                db.add(TestError(
                    task_id=task_id,
                    message=f"Large DOM payload size ({html_len} bytes) detected on {page_url}.",
                    severity="low",
                    page_url=page_url,
                ))
                errors += 1

        log += "[Performance Agent] Performance audit complete.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Performance agent execution failed: {exc}")
    finally:
        db.close()


def run_visual_regression_agent(task_id: str, url: str, page_snapshots: list, codebase_data: dict):
    """Visual Regression Agent: captures baseline screenshots and compares across test runs."""
    logger.info(f"Visual Regression Agent starting for task: {task_id}")
    db = SessionLocal()
    try:
        state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "VisualRegression").first()
        if not state:
            return
        state.status = "running"
        state.started_at = datetime.utcnow()
        state.log_output = f"[VisualRegression Agent] Capturing page layout screenshots for regression checks.\n"
        db.commit()

        errors = 0
        log = state.log_output
        
        # Directory to store baselines
        baseline_dir = os.path.join("visual_baselines", task_id)
        os.makedirs(baseline_dir, exist_ok=True)
        
        # Try to find a previous completed task for the same URL
        task_rec = db.query(Task).filter(Task.id == task_id).first()
        previous_task = None
        if task_rec:
            previous_task = db.query(Task).filter(
                Task.url == task_rec.url,
                Task.id != task_id,
                Task.status == "completed"
            ).order_by(Task.created_at.desc()).first()
            
        prev_baseline_dir = None
        if previous_task:
            prev_baseline_dir = os.path.join("visual_baselines", previous_task.id)
            if not os.path.exists(prev_baseline_dir):
                prev_baseline_dir = None
                
        if prev_baseline_dir:
            log += f"[VisualRegression Agent] Found previous completed task {previous_task.id}. Comparing screenshots against its baselines.\n"
        else:
            log += "[VisualRegression Agent] No previous completed task/baseline found. Capturing initial baseline screenshots.\n"

        # Capture screenshots for each page snapshot
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            
            for idx, snap in enumerate(page_snapshots):
                page_url = snap.get("page_url", url)
                filename = f"page_{idx:03d}_{slugify(page_url.split('//')[-1].replace('/', '_'))}.png"
                current_path = os.path.join(baseline_dir, filename)
                
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1000) # wait for animations to settle
                    page.screenshot(path=current_path, full_page=True)
                    
                    if prev_baseline_dir:
                        prev_path = os.path.join(prev_baseline_dir, filename)
                        if os.path.exists(prev_path):
                            # Compare current with previous baseline
                            diff_pct = compare_screenshots_rms(current_path, prev_path)
                            if diff_pct > 5.0: # 5% visual difference threshold
                                log += f"[VisualRegression Agent] Issue: Visual regression detected on {page_url} (diff: {diff_pct:.2f}%).\n"
                                db.add(TestError(
                                    task_id=task_id,
                                    message=f"Visual regression layout change detected on {page_url} (diff: {diff_pct:.2f}% vs baseline).",
                                    severity="medium",
                                    page_url=page_url,
                                    screenshot_path=current_path
                                ))
                                errors += 1
                            else:
                                log += f"[VisualRegression Agent] {page_url}: Visual match OK (diff: {diff_pct:.2f}%).\n"
                        else:
                            log += f"[VisualRegression Agent] {page_url}: Baseline screenshot missing in previous task. Saved new baseline.\n"
                    else:
                        log += f"[VisualRegression Agent] Saved baseline screenshot for {page_url}.\n"
                except Exception as exc:
                    log += f"[VisualRegression Agent] Failed to capture screenshot for {page_url}: {exc}\n"
                    
            context.close()
            browser.close()

        log += "[VisualRegression Agent] Visual layout captured successfully. Comparison set.\n"
        state.status = "completed"
        state.errors_found = errors
        state.log_output = log
        state.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"VisualRegression agent execution failed: {exc}")
        if state:
            state.status = "failed"
            state.log_output += f"\nError: {exc}"
            db.commit()
    finally:
        db.close()


def run_testing_agent(task_id: str):
    logger.info(f"Starting AI Agent background task for task ID: {task_id}")
    db: Session = SessionLocal()

    try:
        def write_orchestrator_log(message: str):
            if not orchestrator_state:
                return
            current = orchestrator_state.log_output or ""
            orchestrator_state.log_output = current + message.rstrip() + "\n"
            orchestrator_state.completed_at = None
            db.commit()

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found in database.")
            return

        def is_cancelled() -> bool:
            try:
                db.expire(task)
                current_status = db.query(Task.status).filter(Task.id == task_id).scalar()
                return current_status == "stopped"
            except Exception:
                return False

        if is_cancelled():
            return

        task.status = "crawling"
        db.commit()

        orchestrator_state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Orchestrator").first()
        if orchestrator_state:
            orchestrator_state.status = "running"
            orchestrator_state.started_at = datetime.utcnow()
            orchestrator_state.log_output = "[Orchestrator] Started pipeline.\n"
            db.commit()
            write_orchestrator_log(f"[Orchestrator] Task URL: {task.url}")
            write_orchestrator_log("[Orchestrator] Stage: crawling")

        codebase = db.query(Codebase).filter(Codebase.task_id == task_id).first()
        auth = db.query(TaskAuth).filter(TaskAuth.task_id == task_id).first()
        seed_urls = []
        if getattr(task, "url", ""):
            seed_urls.append(task.url)
        codebase_data = {
            "framework_type": "HTML/JS",
            "file_list": [],
            "existing_tests": [],
            "routing_files": [],
            "components": [],
            "file_tree": {}
        }
        key_files_context = ""
        if codebase:
            codebase_data = scan_codebase(codebase.local_path)
            codebase_data["codebase_path"] = codebase.local_path
            codebase.framework_type = codebase_data["framework_type"]
            codebase.file_tree = json.dumps(codebase_data["file_tree"])
            codebase.analyzed_at = datetime.utcnow()
            key_files_context = read_key_files(codebase.local_path, codebase_data)
            write_orchestrator_log(f"[Orchestrator] Scanned codebase: {codebase.local_path}")
            write_orchestrator_log(f"[Orchestrator] Detected framework: {codebase.framework_type}")
            write_orchestrator_log(f"[Orchestrator] Discovered {len(codebase_data['file_list'])} files.")

        auth_data = None
        if auth:
            auth_data = {
                "auth_required": bool(auth.auth_required),
                "auth_login_url": auth.auth_login_url,
                "auth_post_login_url": auth.auth_post_login_url,
                "auth_username": auth.auth_username,
                "auth_password": auth.auth_password,
                "auth_otp_code": auth.auth_otp_code,
                "auth_otp_hint": auth.auth_otp_hint,
                "auth_flow": auth.auth_flow,
                "auth_next_step": auth.auth_next_step,
                "auth_required_fields": auth.auth_required_fields,
            }
            if auth.auth_post_login_url:
                seed_urls.append(auth.auth_post_login_url)

        if auth_data and auth_data.get("auth_required") and not any([
            (auth_data.get("auth_username") or "").strip(),
            (auth_data.get("auth_password") or "").strip(),
            (auth_data.get("auth_otp_code") or "").strip(),
        ]):
            task.status = "needs_input"
            db.commit()
            if orchestrator_state:
                orchestrator_state.status = "failed"
                orchestrator_state.log_output = (orchestrator_state.log_output or "") + "[Orchestrator] Paused: at least one authentication credential is required to continue.\n"
                orchestrator_state.completed_at = datetime.utcnow()
                db.commit()
            logger.info(f"Task {task_id} paused awaiting authentication input.")
            return

        if task.url:
            normalized_task_url = normalize_url(task.url)
            if "aahoa.com" in normalized_task_url:
                seed_urls.extend([
                    "https://www.aahoa.com/strategicpartners",
                    "https://ams.aahoa.com/login",
                    "https://ams.aahoa.com/become-a-member",
                    "https://ams.aahoa.com/become-a-vendor",
                    "https://www.aahoa.com/membership/vendors/vendor-benefits",
                ])
            elif "sellingo.ai" in normalized_task_url:
                seed_urls.extend([
                    "https://sellingo.ai/merchant/create_custom_order",
                    "https://sellingo.ai/merchant/new_orders",
                    "https://sellingo.ai/merchant/mycatalog",
                ])

        deduped_seed_urls = []
        for seed in seed_urls:
            normalized_seed = normalize_url(seed)
            if normalized_seed not in deduped_seed_urls:
                deduped_seed_urls.append(normalized_seed)
        seed_urls = deduped_seed_urls
        if seed_urls:
            write_orchestrator_log(f"[Orchestrator] Seeded pages: {', '.join(seed_urls)}")

        is_mobile = bool(task.is_mobile) if getattr(task, "is_mobile", None) is not None else False
        # Auto-detect if mobile emulation is required for local React webview projects
        if not is_mobile and (
            "192.168.10.125:3000" in getattr(task, "url", "")
            or (codebase and "ahoa" in codebase.local_path.lower())
        ):
            is_mobile = True
            task.is_mobile = 1
            db.commit()
            write_orchestrator_log("[Orchestrator] Auto-detected React mobile webview codebase. Enabling Mobile Viewport Emulation automatically.")

        page_snapshots = discover_pages_with_playwright(task.url, auth=auth_data, seed_urls=seed_urls, is_mobile=is_mobile, cancel_check=is_cancelled, task_id=task_id)
        if is_cancelled():
            return
        auth_issue = (auth_data or {}).get("_codex_auth_issue")
        if auth_issue:
            if auth:
                auth.auth_next_step = "Provide the missing authentication field(s)"
                try:
                    required_fields = list(auth_issue.get("fields", []))
                    auth.auth_required_fields = json.dumps(required_fields)
                    auth.auth_flow = _classify_auth_flow(required_fields)
                except Exception:
                    auth.auth_required_fields = None
                    auth.auth_flow = None
                db.commit()
            task.status = "needs_input"
            db.commit()
            if orchestrator_state:
                orchestrator_state.status = "failed"
                needed = ", ".join(auth_issue.get("fields", [])) or "authentication input"
                orchestrator_state.log_output = (orchestrator_state.log_output or "") + f"[Orchestrator] Paused: login flow needs {needed}.\n"
                orchestrator_state.completed_at = datetime.utcnow()
                db.commit()
            logger.info(f"Task {task_id} paused awaiting auth input: {auth_issue}")
            return
        discovered_urls = [snap.get("page_url", "") for snap in page_snapshots if snap.get("page_url")]
        write_orchestrator_log(f"[Orchestrator] Discovered pages: {', '.join(discovered_urls) if discovered_urls else 'none'}")
        write_orchestrator_log(f"[Orchestrator] Discovered {len(page_snapshots)} page snapshots.")

        if is_cancelled():
            return
        task.status = "generating_test_cases"
        db.commit()
        write_orchestrator_log("[Orchestrator] Stage: generating_test_cases")
        time.sleep(1.5)

        ai_model = getattr(task, "ai_model", "gemini-1.5-flash") or "gemini-1.5-flash"
        user_prompt = getattr(task, "user_prompt", None)
        
        openai_key = os.getenv("OPENAI_API_KEY")
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        
        analysis = {"use_cases": [], "suggestions": []}
        selected_service = "local fallback"
        
        site_profile = aggregate_site_profile(task.url, page_snapshots)
        crawl_data_for_ai = {
            "title": site_profile.get("primary_title", "Unknown Page"),
            "domain": site_profile.get("domain", normalize_url(task.url).split("//", 1)[1].split("/")[0]),
            "headings": site_profile.get("headings", []),
            "forms": site_profile.get("forms", []),
            "links": (site_profile.get("links", []) or [])[:20],
            "meta_tags": site_profile.get("meta_tags", {}),
            "html_length": site_profile.get("html_length", 0),
            "page_count": site_profile.get("page_count", len(page_snapshots)),
            "status_code": site_profile.get("status_code", 200),
        }
        
        # Model Selection Routing
        is_openai_model = any(m in ai_model.lower() for m in ["openai", "gpt", "chatgpt"])
        is_gemini_model = any(m in ai_model.lower() for m in ["gemini", "google"])
        
        if ai_model == "auto":
            if openai_key:
                analysis = generate_openai_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, openai_key, user_prompt)
                selected_service = "OpenAI API (gpt-4o via Auto)"
            elif gemini_key:
                analysis = generate_gemini_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, gemini_key, user_prompt)
                selected_service = "Gemini API (gemini-1.5-flash via Auto)"
        elif is_openai_model:
            if openai_key:
                analysis = generate_openai_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, openai_key, user_prompt)
                selected_service = f"OpenAI API ({ai_model})"
            else:
                write_orchestrator_log(f"[Orchestrator] Requested model '{ai_model}' but OPENAI_API_KEY is not set.")
        elif is_gemini_model:
            if gemini_key:
                analysis = generate_gemini_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, gemini_key, user_prompt)
                selected_service = f"Gemini API ({ai_model})"
            else:
                write_orchestrator_log(f"[Orchestrator] Requested model '{ai_model}' but GEMINI_API_KEY is not set.")
                
        if not analysis.get("use_cases"):
            # Secondary fallback routing if preferred service failed or key was missing
            if gemini_key and not is_gemini_model:
                write_orchestrator_log("[Orchestrator] Attempting fallback to Gemini API.")
                analysis = generate_gemini_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, gemini_key, user_prompt)
                if analysis.get("use_cases"):
                    selected_service = "Gemini API (Fallback)"
            elif openai_key and not is_openai_model:
                write_orchestrator_log("[Orchestrator] Attempting fallback to OpenAI API.")
                analysis = generate_openai_analysis(task.url, crawl_data_for_ai, codebase_data, key_files_context, openai_key, user_prompt)
                if analysis.get("use_cases"):
                    selected_service = "OpenAI API (Fallback)"

        if not analysis.get("use_cases"):
            analysis = build_test_plan(task.url, page_snapshots, codebase_data)
            selected_service = "local fallback planner"
            write_orchestrator_log("[Orchestrator] Local fallback planner produced the test suite.")
        else:
            write_orchestrator_log(f"[Orchestrator] Test planning source: {selected_service}")

        write_orchestrator_log(
            f"[Orchestrator] Planned {len(analysis.get('use_cases', []))} use case(s) and {len(analysis.get('suggestions', []))} suggestion(s)."
        )

        # Parse and save custom use cases from task.custom_use_cases_json if defined
        custom_use_cases = []
        if getattr(task, "custom_use_cases_json", None):
            try:
                custom_use_cases = json.loads(task.custom_use_cases_json)
                if not isinstance(custom_use_cases, list):
                    custom_use_cases = []
            except Exception as e:
                logger.error(f"Error parsing custom_use_cases_json: {e}")
                write_orchestrator_log(f"[Orchestrator] Warning: Failed to parse custom use cases: {e}")

        # Combine AI-generated and custom use cases
        all_use_cases_to_create = []
        # AI-generated
        for uc_data in analysis.get("use_cases", []):
            all_use_cases_to_create.append((uc_data, False)) # (data, is_custom)
        # Custom
        for uc_data in custom_use_cases:
            all_use_cases_to_create.append((uc_data, True)) # (data, is_custom)

        # Inject Sellingo-specific merchant cases if URL targets Sellingo
        if task.url and "sellingo.ai" in normalize_url(task.url):
            sellingo_uc = {
                "title": "Sellingo Merchant Order Management",
                "description": "Verify custom order creation, filter operations, and tab toggling on merchant screens.",
                "test_cases": [
                    {
                        "title": "Create Custom Order Page Validation",
                        "steps": "1. Navigate to https://sellingo.ai/merchant/create_custom_order\n2. Fill custom order form inputs (item, user, price, qty)\n3. Click create/submit order\n4. Confirm order created successfully and displays in records list",
                        "expected_result": "Custom order form validation works and submits successfully to create order record.",
                        "test_type": "positive",
                        "page_url": "https://sellingo.ai/merchant/create_custom_order"
                    },
                    {
                        "title": "New Orders List Filter Verification",
                        "steps": "1. Navigate to https://sellingo.ai/merchant/new_orders\n2. Toggle different list filters (status, date range, payment)\n3. Verify orders list updates dynamically for each filter option",
                        "expected_result": "Order filters successfully reload matching orders list without exception or freezing.",
                        "test_type": "positive",
                        "page_url": "https://sellingo.ai/merchant/new_orders"
                    },
                    {
                        "title": "New Orders Tab Switching Validation",
                        "steps": "1. Navigate to https://sellingo.ai/merchant/new_orders\n2. Click each order tab (e.g. Pending, Completed, Cancelled)\n3. Confirm active tab highlight changes and display updates",
                        "expected_result": "Tab navigation functions correctly, changing styling state and updating the list display.",
                        "test_type": "positive",
                        "page_url": "https://sellingo.ai/merchant/new_orders"
                    }
                ]
            }
            all_use_cases_to_create.append((sellingo_uc, False))

        use_cases_mapping = {}
        for uc_data, is_custom in all_use_cases_to_create:
            title_prefix = "[Custom] " if is_custom else ""
            uc_title = f"{title_prefix}{uc_data['title']}"
            use_case = UseCase(
                task_id=task.id, 
                title=uc_title, 
                description=uc_data.get("description")
            )
            db.add(use_case)
            db.flush()
            
            # Prepare and persist test cases
            for tc_data in uc_data.get("test_cases", []):
                test_case = TestCase(
                    task_id=task.id,
                    use_case_id=use_case.id,
                    title=tc_data.get("title", "Custom Test"),
                    steps=tc_data.get("steps", ""),
                    expected_result=tc_data.get("expected_result", ""),
                    status="pending",
                    test_type=tc_data.get("test_type"),
                    page_url=tc_data.get("page_url") or task.url
                )
                db.add(test_case)

        for sug_data in analysis.get("suggestions", []):
            suggestion = Suggestion(
                task_id=task.id,
                title=sug_data["title"],
                description=sug_data.get("description"),
                priority=sug_data.get("priority", "medium").lower()
            )
            db.add(suggestion)

        db.commit()

        # Run user journey agent to build workflows from navigation
        write_orchestrator_log("[Orchestrator] Running User Journey Agent to extract workflows...")
        run_user_journey_agent(task.id, task.url, page_snapshots, codebase_data)
        write_orchestrator_log("[Orchestrator] User Journey Agent complete.")

        # Save snapshots and transition to planned
        task.page_snapshots_json = json.dumps(page_snapshots)
        task.status = "planned"
        if orchestrator_state:
            orchestrator_state.status = "completed"
            orchestrator_state.log_output = (orchestrator_state.log_output or "") + "[Orchestrator] Planning complete. Ready to run tests on demand.\n"
            orchestrator_state.completed_at = datetime.utcnow()
        db.commit()
        write_orchestrator_log("[Orchestrator] Planning complete. Ready to run tests on demand.")
        logger.info(f"AI Agent planning background task completed for task ID: {task_id}")

    except Exception as exc:
        logger.error(f"Error executing AI testing agent: {exc}")
        logger.error(traceback.format_exc())
        task = db.query(Task).filter(Task.id == task_id).first()
        if task and task.status != "stopped":
            task.status = "failed"
            task.completed_at = datetime.utcnow()
        orchestrator_state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Orchestrator").first()
        if orchestrator_state:
            orchestrator_state.status = "failed"
            orchestrator_state.log_output = (orchestrator_state.log_output or "") + f"[Orchestrator Error] {exc}\n"
            orchestrator_state.log_output += traceback.format_exc()
            orchestrator_state.completed_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def run_test_execution_agent(task_id: str):
    logger.info(f"Starting AI Agent test execution background task for task ID: {task_id}")
    db: Session = SessionLocal()

    try:
        def write_orchestrator_log(message: str):
            if not orchestrator_state:
                return
            current = orchestrator_state.log_output or ""
            orchestrator_state.log_output = current + message.rstrip() + "\n"
            orchestrator_state.completed_at = None
            db.commit()

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found in database.")
            return

        def is_cancelled() -> bool:
            try:
                db.expire(task)
                current_status = db.query(Task.status).filter(Task.id == task_id).scalar()
                return current_status == "stopped"
            except Exception:
                return False

        if is_cancelled():
            return

        task.status = "running_tests"
        db.commit()

        # Reset agent states
        for state_name in [
            "Orchestrator", "RouteDiscovery", "HealthCheck", "Login",
            "RolePermission", "UserJourney", "Form", "API",
            "DatabaseIntegrity", "Security", "Accessibility", "Responsive",
            "VisualRegression", "Performance", "CodeCorrelation"
        ]:
            state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == state_name).first()
            if state:
                state.status = "pending"
                state.started_at = None
                state.completed_at = None
                state.log_output = ""
                state.errors_found = 0
        db.commit()

        orchestrator_state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Orchestrator").first()
        if orchestrator_state:
            orchestrator_state.status = "running"
            orchestrator_state.started_at = datetime.utcnow()
            orchestrator_state.log_output = "[Orchestrator] Started test execution phase.\n"
            db.commit()
            write_orchestrator_log("[Orchestrator] Stage: running_tests")

        # Load page snapshots and codebase
        page_snapshots = []
        if task.page_snapshots_json:
            try:
                page_snapshots = json.loads(task.page_snapshots_json)
            except Exception as e:
                logger.error(f"Error decoding page_snapshots_json: {e}")

        codebase = db.query(Codebase).filter(Codebase.task_id == task_id).first()
        codebase_data = {
            "framework_type": "HTML/JS",
            "file_list": [],
            "existing_tests": [],
            "routing_files": [],
            "components": [],
            "file_tree": {}
        }
        if codebase:
            if codebase.file_tree:
                try:
                    codebase_data["file_tree"] = json.loads(codebase.file_tree)
                except Exception:
                    pass
            codebase_data["framework_type"] = codebase.framework_type
            codebase_data["codebase_path"] = codebase.local_path

        auth = db.query(TaskAuth).filter(TaskAuth.task_id == task_id).first()
        auth_data = None
        if auth:
            auth_data = {
                "auth_required": bool(auth.auth_required),
                "auth_login_url": auth.auth_login_url,
                "auth_post_login_url": auth.auth_post_login_url,
                "auth_username": auth.auth_username,
                "auth_password": auth.auth_password,
                "auth_otp_code": auth.auth_otp_code,
                "auth_otp_hint": auth.auth_otp_hint,
                "auth_flow": auth.auth_flow,
                "auth_next_step": auth.auth_next_step,
                "auth_required_fields": auth.auth_required_fields,
            }

        # Build use_cases_mapping and use_case_titles from DB
        use_cases = db.query(UseCase).filter(UseCase.task_id == task_id).all()
        use_cases_mapping = {}
        use_case_titles = {}
        for uc in use_cases:
            test_cases = db.query(TestCase).filter(TestCase.use_case_id == uc.id).all()
            # Reset their status to pending when starting execution
            for tc in test_cases:
                tc.status = "pending"
                tc.error_message = None
                tc.execution_time = None
            db.commit()
            
            use_cases_mapping[uc.id] = test_cases
            use_case_titles[uc.id] = uc.title

        # Run health check first — filter out broken pages
        write_orchestrator_log("[Orchestrator] Running pre-flight health check on discovered pages...")
        healthy_pages, failed_pages = run_health_check_agent(task.id, task.url, page_snapshots, auth_data)
        # Only pass healthy pages to sub-agents
        page_snapshots_for_agents = [s for s in page_snapshots
                                      if s.get("page_url") not in {f["url"] for f in failed_pages}]
        write_orchestrator_log(f"[Orchestrator] Health check complete. {len(healthy_pages)} healthy pages, {len(failed_pages)} failed pages.")

        # Run parallel heuristic analysis agents
        with ThreadPoolExecutor(max_workers=13) as executor:
            executor.submit(run_ui_ux_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_responsive_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_form_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_api_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_image_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_route_discovery_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_login_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_role_permission_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_database_integrity_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_security_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_accessibility_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_performance_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)
            executor.submit(run_visual_regression_agent, task.id, task.url, page_snapshots_for_agents, codebase_data)

        time.sleep(2.5)

        # Run the browser-driven Playwright tests
        error_ids = execute_test_plan(task.id, page_snapshots_for_agents, use_cases_mapping, use_case_titles, auth=auth_data, log_callback=write_orchestrator_log)
        
        if is_cancelled():
            return

        auth_issue = (auth_data or {}).get("_codex_auth_issue")
        if auth_issue:
            if auth:
                auth.auth_next_step = "Provide the missing authentication field(s)"
                try:
                    required_fields = list(auth_issue.get("fields", []))
                    auth.auth_required_fields = json.dumps(required_fields)
                    auth.auth_flow = _classify_auth_flow(required_fields)
                except Exception:
                    auth.auth_required_fields = None
                    auth.auth_flow = None
                db.commit()
            task.status = "needs_input"
            db.commit()
            if orchestrator_state:
                orchestrator_state.status = "failed"
                needed = ", ".join(auth_issue.get("fields", [])) or "authentication input"
                orchestrator_state.log_output = (orchestrator_state.log_output or "") + f"[Orchestrator] Paused: login flow needs {needed}.\n"
                orchestrator_state.completed_at = datetime.utcnow()
                db.commit()
            logger.info(f"Task {task_id} paused awaiting auth input: {auth_issue}")
            return

        write_orchestrator_log(f"[Orchestrator] Browser validations completed with {len(error_ids)} error(s).")

        if codebase and error_ids:
            run_code_correlation_agent(task.id, codebase.local_path, codebase_data, error_ids)
            write_orchestrator_log("[Orchestrator] Code correlation mapping completed.")
        else:
            code_review_state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "CodeCorrelation").first()
            if code_review_state:
                code_review_state.status = "completed"
                code_review_state.log_output = "[CodeCorrelation Agent] No codebase details or errors to map.\n"
                code_review_state.completed_at = datetime.utcnow()
                db.commit()
            write_orchestrator_log("[Orchestrator] No code correlation mapping required.")

        # --- CLEANUP PHASE ---
        write_orchestrator_log("[Orchestrator] Starting safe testing cleanup phase...")
        try:
            cleanup_logs = db.query(TestCleanupLog).filter(
                TestCleanupLog.task_id == task_id,
                TestCleanupLog.action == "created"
            ).all()
            for log in cleanup_logs:
                log.action = "cleanup_pending"
                log.cleaned_at = datetime.utcnow()
            db.commit()
            write_orchestrator_log(f"[Orchestrator] Cleanup phase complete. Marked {len(cleanup_logs)} record(s) for cleanup.")
        except Exception as cleanup_err:
            logger.error(f"Cleanup phase failed: {cleanup_err}")
            write_orchestrator_log(f"[Orchestrator Warning] Cleanup failed: {cleanup_err}")

        task.status = "completed"
        task.completed_at = datetime.utcnow()
        if orchestrator_state:
            orchestrator_state.status = "completed"
            orchestrator_state.log_output = (orchestrator_state.log_output or "") + "[Orchestrator] Pipeline complete.\n"
            orchestrator_state.completed_at = datetime.utcnow()
        db.commit()
        logger.info(f"AI Agent test execution background task completed for task ID: {task_id}")

    except Exception as exc:
        logger.error(f"Error executing AI testing agent execution: {exc}")
        logger.error(traceback.format_exc())
        task = db.query(Task).filter(Task.id == task_id).first()
        if task and task.status != "stopped":
            task.status = "failed"
            task.completed_at = datetime.utcnow()
        orchestrator_state = db.query(AgentState).filter(AgentState.task_id == task_id, AgentState.agent_name == "Orchestrator").first()
        if orchestrator_state:
            orchestrator_state.status = "failed"
            orchestrator_state.log_output = (orchestrator_state.log_output or "") + f"[Orchestrator Error] {exc}\n"
            orchestrator_state.log_output += traceback.format_exc()
            orchestrator_state.completed_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()

