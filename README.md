# AI Website Testing Automation Backend

This directory houses the Python FastAPI backend for the Multi-Agent Web Testing & Codebase Review Platform.

For full architectural details, database schemas, API references, and agent pipelines description, please read the main [BACKEND_DOCUMENTATION.md](file:///home/dev04/datagrid/AI_testing/BACKEND_DOCUMENTATION.md) in the project root.

---

## Quickstart Guide

### 1. Prerequisites
Ensure you have Python 3.10+ installed and the PostgreSQL docker container running:
```bash
# Start PostgreSQL via docker-compose (from the project root)
docker-compose up -d
```

### 2. Environment Setup
Create a `.env` file in this directory (`BackEND_project`):
```env
DATABASE_URL=sqlite:///./testing_automation.db
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/testing_automation

GEMINI_API_KEY=your_gemini_key_here
```

### 3. Local Installation & Run
Create the virtual environment, install requirements, set up playwright, and launch the dev server:
```bash
# Create and activate environment
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Install Playwright Chromium browser
playwright install chromium

# Launch Uvicorn dev server
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

### 4. Running Diagnostic Scripts
Various diagnostic scripts are available to test parts of the system independently:
```bash
# Validate agents pipeline logic
python test_agent.py

# Diagnose browser automation login flow
python diagnose_login.py

# Debug Cloudflare/captcha blocks on target URLs
python debug_blocked_pages.py
```
