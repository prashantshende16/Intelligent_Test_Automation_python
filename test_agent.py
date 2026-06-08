import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from agent import build_test_plan, read_key_files, scan_codebase


class BuildTestPlanTests(unittest.TestCase):
    def test_build_test_plan_generates_navigation_and_form_cases(self):
        pages = [
            {
                "page_url": "https://example.com",
                "title": "Example Site",
                "headings": ["h1: Welcome to Example Site"],
                "links": [{"href": "/contact", "text": "Contact"}],
                "forms": [
                    {
                        "action": "/subscribe",
                        "inputs": [
                            {"name": "email", "placeholder": "Email", "required": False}
                        ]
                    }
                ],
                "meta_tags": {"description": "Example"},
                "html_snippet": "<html><h1>Welcome</h1><form><input name='email'></form></html>"
            }
        ]

        result = build_test_plan("https://example.com", pages, {"framework_type": "React"})

        self.assertGreaterEqual(len(result["use_cases"]), 2)
        self.assertGreaterEqual(len(result["suggestions"]), 1)
        test_titles = [tc["title"] for use_case in result["use_cases"] for tc in use_case["test_cases"]]
        self.assertIn("Required Field Validation - 1 Fields", test_titles)

    def test_build_test_plan_handles_server_error(self):
        pages = [
            {
                "page_url": "https://example.com",
                "title": "Error",
                "headings": [],
                "links": [],
                "forms": [],
                "meta_tags": {},
                "html_snippet": "",
                "status_code": 500
            }
        ]

        result = build_test_plan("https://example.com", pages, {"framework_type": "HTML/JS"})

        self.assertIn("example.com", result["use_cases"][0]["title"])
        self.assertEqual(result["use_cases"][0]["test_cases"][0]["status"], "pending")
        self.assertEqual(result["use_cases"][0]["test_cases"][0]["check_type"], "page_load")

    def test_build_test_plan_differs_between_sites(self):
        rich_site = [{
            "page_url": "https://shop.example.com",
            "title": "Shop Example",
            "headings": ["h1: Shop", "h2: Products"],
            "links": [{"href": "/cart", "text": "Cart"}, {"href": "/about", "text": "About"}],
            "forms": [{"action": "/login", "inputs": [{"name": "email", "required": True}]}],
            "meta_tags": {"description": "shop", "viewport": "width=device-width", "og:title": "Shop"},
            "html_length": 45000,
            "html_snippet": "<html>shop</html>",
            "images": [{"src": "/logo.png", "alt": "Logo", "has_alt": True}],
            "status_code": 200,
        }]
        thin_site = [{
            "page_url": "https://minimal.example.org",
            "title": "Minimal",
            "headings": [],
            "links": [],
            "forms": [],
            "meta_tags": {},
            "html_length": 900,
            "html_snippet": "<html>minimal</html>",
            "images": [],
            "status_code": 200,
        }]

        rich_plan = build_test_plan("https://shop.example.com", rich_site, {})
        thin_plan = build_test_plan("https://minimal.example.org", thin_site, {})

        rich_titles = [tc["title"] for uc in rich_plan["use_cases"] for tc in uc["test_cases"]]
        thin_titles = [tc["title"] for uc in thin_plan["use_cases"] for tc in uc["test_cases"]]
        self.assertNotEqual(rich_titles, thin_titles)
        self.assertTrue(any("shop.example.com" in title for title in rich_titles))
        self.assertTrue(any("minimal.example.org" in title for title in thin_titles))


class CodebaseScannerTests(unittest.TestCase):
    def test_scan_codebase_detects_react_vite_and_key_files(self):
        with tempfile.TemporaryDirectory() as project_dir:
            os.makedirs(os.path.join(project_dir, "src", "components"))
            os.makedirs(os.path.join(project_dir, "node_modules", "ignored"))

            with open(os.path.join(project_dir, "package.json"), "w", encoding="utf-8") as package_file:
                json.dump({"dependencies": {"react": "19.0.0"}, "devDependencies": {"vite": "5.0.0"}}, package_file)
            with open(os.path.join(project_dir, "src", "App.jsx"), "w", encoding="utf-8") as app_file:
                app_file.write("export default function App() { return <main>Hello</main>; }\n")
            with open(os.path.join(project_dir, "src", "components", "LoginForm.jsx"), "w", encoding="utf-8") as form_file:
                form_file.write("export function LoginForm() { return <form><input /></form>; }\n")
            with open(os.path.join(project_dir, "src", "App.test.jsx"), "w", encoding="utf-8") as test_file:
                test_file.write("test('renders', () => {});\n")
            with open(os.path.join(project_dir, "node_modules", "ignored", "Huge.jsx"), "w", encoding="utf-8") as ignored_file:
                ignored_file.write("ignored\n")

            result = scan_codebase(project_dir)
            context = read_key_files(project_dir, result)

        self.assertEqual(result["framework_type"], "React + Vite")
        self.assertIn("src/App.jsx", result["routing_files"])
        self.assertIn("src/components/LoginForm.jsx", result["components"])
        self.assertIn("src/App.test.jsx", result["existing_tests"])
        self.assertNotIn("node_modules/ignored/Huge.jsx", result["file_list"])
        self.assertIn("--- src/App.jsx ---", context)


if __name__ == "__main__":
    unittest.main()
