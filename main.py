import threading
import os
import json
import csv
import io
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, text, inspect
from typing import List

from database import engine, Base, get_db
import models
import schemas
from agent import run_testing_agent

# Initialize Database tables
Base.metadata.create_all(bind=engine)

def ensure_task_auth_columns():
    inspector = inspect(engine)
    if "task_auths" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("task_auths")}
    if "auth_post_login_url" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE task_auths ADD COLUMN auth_post_login_url VARCHAR"))

ensure_task_auth_columns()

app = FastAPI(title="AI Website Testing Automation API")

# Enable CORS for frontend app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In development, allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_task_with_counts(task: models.Task, db: Session) -> schemas.TaskResponse:
    """Helper function to calculate counts for a single task."""
    use_case_count = db.query(func.count(models.UseCase.id)).filter(models.UseCase.task_id == task.id).scalar() or 0
    test_case_count = db.query(func.count(models.TestCase.id)).filter(models.TestCase.task_id == task.id).scalar() or 0
    error_count = db.query(func.count(models.TestError.id)).filter(models.TestError.task_id == task.id).scalar() or 0
    suggestion_count = db.query(func.count(models.Suggestion.id)).filter(models.Suggestion.task_id == task.id).scalar() or 0
    
    return schemas.TaskResponse(
        id=task.id,
        url=task.url,
        status=task.status,
        created_at=task.created_at,
        completed_at=task.completed_at,
        use_case_count=use_case_count,
        test_case_count=test_case_count,
        error_count=error_count,
        suggestion_count=suggestion_count
    )

def start_task_thread(task_id: str) -> None:
    thread = threading.Thread(target=run_testing_agent, args=(task_id,))
    thread.daemon = True
    thread.start()

@app.post("/api/tasks", response_model=schemas.TaskResponse)
def create_task(task_in: schemas.TaskCreate, db: Session = Depends(get_db)):
    # Validate codebase path if provided
    if task_in.codebase_path:
        abs_path = os.path.abspath(task_in.codebase_path)
        if not os.path.isdir(abs_path):
            raise HTTPException(status_code=400, detail=f"Codebase directory path '{task_in.codebase_path}' does not exist locally.")
            
    # Create Task record
    db_task = models.Task(
        url=task_in.url,
        status="pending"
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    
    # Save codebase details if provided
    if task_in.codebase_path:
        abs_path = os.path.abspath(task_in.codebase_path)
        db_codebase = models.Codebase(
            task_id=db_task.id,
            local_path=abs_path,
            framework_type="React"
        )
        db.add(db_codebase)

    if getattr(task_in, "auth_required", False):
        db_auth = models.TaskAuth(
            task_id=db_task.id,
            auth_required=1,
            auth_login_url=task_in.auth_login_url,
            auth_post_login_url=task_in.auth_post_login_url,
            auth_username=task_in.auth_username,
            auth_password=task_in.auth_password,
            auth_otp_code=task_in.auth_otp_code,
            auth_otp_hint=task_in.auth_otp_hint,
        )
        db.add(db_auth)

    seed_urls = []
    if getattr(task_in, "seed_urls", None):
        seed_urls = [seed.strip() for seed in task_in.seed_urls if seed and seed.strip()]
    if seed_urls:
        db_seed = models.TaskSeed(
            task_id=db_task.id,
            seed_urls_json=json.dumps(seed_urls)
        )
        db.add(db_seed)
        
    # Pre-populate Agent states for UI tracking
    for agent_name in ["Orchestrator", "UI_UX", "Responsive", "Form", "API", "Image", "CodeReview"]:
        agent_state = models.AgentState(
            task_id=db_task.id,
            agent_name=agent_name,
            status="pending"
        )
        db.add(agent_state)
        
    db.commit()
    db.refresh(db_task)
    
    # Spawn background thread to run AI Testing Agent
    start_task_thread(db_task.id)
    
    return get_task_with_counts(db_task, db)

@app.patch("/api/tasks/{task_id}/input", response_model=schemas.TaskResponse)
def update_task_input(task_id: str, task_input: schemas.TaskInputUpdate, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    auth = db.query(models.TaskAuth).filter(models.TaskAuth.task_id == task_id).first()
    if task_input.auth_required is not None:
        if not auth:
            auth = models.TaskAuth(task_id=task_id, auth_required=int(bool(task_input.auth_required)))
            db.add(auth)
        auth.auth_required = int(bool(task_input.auth_required))
    if auth:
        if task_input.auth_login_url is not None:
            auth.auth_login_url = task_input.auth_login_url
        if task_input.auth_post_login_url is not None:
            auth.auth_post_login_url = task_input.auth_post_login_url
        if task_input.auth_username is not None:
            auth.auth_username = task_input.auth_username
        if task_input.auth_password is not None:
            auth.auth_password = task_input.auth_password
        if task_input.auth_otp_code is not None:
            auth.auth_otp_code = task_input.auth_otp_code
        if task_input.auth_otp_hint is not None:
            auth.auth_otp_hint = task_input.auth_otp_hint

    if task_input.form_values_json is not None:
        form_data = db.query(models.TaskFormData).filter(models.TaskFormData.task_id == task_id).first()
        if not form_data:
            form_data = models.TaskFormData(task_id=task_id, form_values_json=task_input.form_values_json)
            db.add(form_data)
        else:
            form_data.form_values_json = task_input.form_values_json

    if task_input.seed_urls is not None:
        seed_urls = [seed.strip() for seed in task_input.seed_urls if seed and seed.strip()]
        seed_record = db.query(models.TaskSeed).filter(models.TaskSeed.task_id == task_id).first()
        if not seed_record:
            seed_record = models.TaskSeed(task_id=task_id, seed_urls_json=json.dumps(seed_urls))
            db.add(seed_record)
        else:
            seed_record.seed_urls_json = json.dumps(seed_urls)

    task.status = "pending"
    task.completed_at = None
    db.commit()
    db.refresh(task)
    return get_task_with_counts(task, db)

@app.post("/api/tasks/{task_id}/resume", response_model=schemas.TaskResponse)
def resume_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = "pending"
    task.completed_at = None
    db.commit()
    start_task_thread(task_id)
    return get_task_with_counts(task, db)

@app.get("/api/tasks", response_model=List[schemas.TaskResponse])
def list_tasks(db: Session = Depends(get_db)):
    tasks = db.query(models.Task).order_by(models.Task.created_at.desc()).all()
    return [get_task_with_counts(task, db) for task in tasks]

@app.get("/api/tasks/{task_id}", response_model=schemas.TaskResponse)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return get_task_with_counts(task, db)

@app.get("/api/tasks/{task_id}/details", response_model=schemas.TaskDetailsResponse)
def get_task_details(task_id: str, db: Session = Depends(get_db)):
    # Get task
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    task_resp = get_task_with_counts(task, db)
    
    # Fetch related records
    use_cases = db.query(models.UseCase).filter(models.UseCase.task_id == task_id).order_by(models.UseCase.created_at.asc()).all()
    test_cases = db.query(models.TestCase).filter(models.TestCase.task_id == task_id).order_by(models.TestCase.created_at.asc()).all()
    errors = db.query(models.TestError).filter(models.TestError.task_id == task_id).order_by(models.TestError.created_at.desc()).all()
    suggestions = db.query(models.Suggestion).filter(models.Suggestion.task_id == task_id).order_by(models.Suggestion.created_at.asc()).all()
    
    codebase = db.query(models.Codebase).filter(models.Codebase.task_id == task_id).first()
    auth = db.query(models.TaskAuth).filter(models.TaskAuth.task_id == task_id).first()
    seeds = db.query(models.TaskSeed).filter(models.TaskSeed.task_id == task_id).first()
    agent_states = db.query(models.AgentState).filter(models.AgentState.task_id == task_id).order_by(models.AgentState.started_at.asc()).all()
    
    return schemas.TaskDetailsResponse(
        task=task_resp,
        use_cases=use_cases,
        test_cases=test_cases,
        errors=errors,
        suggestions=suggestions,
        codebase=codebase,
        auth=auth,
        seeds=seeds,
        agent_states=agent_states
    )

@app.get("/api/tasks/{task_id}/report.csv")
def download_task_report(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    use_cases = db.query(models.UseCase).filter(models.UseCase.task_id == task_id).all()
    test_cases = db.query(models.TestCase).filter(models.TestCase.task_id == task_id).all()
    errors = db.query(models.TestError).filter(models.TestError.task_id == task_id).all()
    suggestions = db.query(models.Suggestion).filter(models.Suggestion.task_id == task_id).all()
    codebase = db.query(models.Codebase).filter(models.Codebase.task_id == task_id).first()
    auth = db.query(models.TaskAuth).filter(models.TaskAuth.task_id == task_id).first()
    seeds = db.query(models.TaskSeed).filter(models.TaskSeed.task_id == task_id).first()
    agent_states = db.query(models.AgentState).filter(models.AgentState.task_id == task_id).all()

    output = io.StringIO()
    writer = csv.writer(output)

    def section(title: str):
        writer.writerow([])
        writer.writerow([title])

    def kv_rows(pairs):
        for key, value in pairs:
            writer.writerow([key, value])

    section("Task Summary")
    kv_rows([
        ("Task ID", task.id),
        ("URL", task.url),
        ("Status", task.status),
        ("Created At", task.created_at.isoformat()),
        ("Completed At", task.completed_at.isoformat() if task.completed_at else ""),
        ("Test Cases", len(test_cases)),
        ("Errors", len(errors)),
        ("Suggestions", len(suggestions)),
    ])

    section("Codebase")
    if codebase:
        kv_rows([
            ("Path", codebase.local_path),
            ("Framework", codebase.framework_type),
            ("Analyzed At", codebase.analyzed_at.isoformat() if codebase.analyzed_at else ""),
        ])
    else:
        writer.writerow(["Not provided"])

    section("Authentication")
    if auth:
        kv_rows([
            ("Auth Required", str(bool(auth.auth_required))),
            ("Login URL", auth.auth_login_url or ""),
            ("Username", auth.auth_username or ""),
            ("OTP Hint", auth.auth_otp_hint or ""),
        ])
    else:
        writer.writerow(["Not provided"])

    section("Seed URLs")
    if seeds and seeds.seed_urls_json:
        try:
            seed_list = json.loads(seeds.seed_urls_json)
        except Exception:
            seed_list = [seeds.seed_urls_json]
        for seed in seed_list:
            writer.writerow([seed])
    else:
        writer.writerow(["Not provided"])

    section("Use Cases")
    writer.writerow(["Use Case Title", "Page URL", "Description", "Created At", "Use Case ID"])
    test_case_page_urls = {}
    for err in errors:
        if err.test_case_id and err.page_url:
            test_case_page_urls[err.test_case_id] = err.page_url
    use_case_page_urls = {}
    for tc in test_cases:
        if not tc.use_case_id:
            continue
        page_url = test_case_page_urls.get(tc.id)
        if page_url:
            use_case_page_urls.setdefault(tc.use_case_id, page_url)
    for err in errors:
        if not err.test_case_id or not err.page_url:
            continue
        tc = next((item for item in test_cases if item.id == err.test_case_id), None)
        if tc and tc.use_case_id:
            use_case_page_urls.setdefault(tc.use_case_id, err.page_url)
    for uc in use_cases:
        writer.writerow([uc.title, use_case_page_urls.get(uc.id, ""), uc.description or "", uc.created_at.isoformat(), uc.id])

    section("Test Cases")
    writer.writerow(["Title", "Status", "Page URL", "Expected Result", "Error Message", "Steps", "Execution Time", "Created At", "Use Case ID"])
    for tc in test_cases:
        writer.writerow([
            tc.title,
            tc.status,
            test_case_page_urls.get(tc.id, ""),
            tc.expected_result or "",
            tc.error_message or "",
            tc.steps or "",
            tc.execution_time if tc.execution_time is not None else "",
            tc.created_at.isoformat(),
            tc.use_case_id or "",
        ])

    section("Errors")
    writer.writerow(["Message", "Severity", "Page URL", "Screenshot", "Created At", "Test Case ID"])
    for err in errors:
        writer.writerow([
            err.message,
            err.severity,
            err.page_url,
            err.screenshot_path or "",
            err.created_at.isoformat(),
            err.test_case_id or "",
        ])

    section("Suggestions")
    writer.writerow(["Title", "Priority", "Description", "Created At"])
    for sug in suggestions:
        writer.writerow([sug.title, sug.priority, sug.description or "", sug.created_at.isoformat()])

    section("Agent States")
    writer.writerow(["Agent", "Status", "Warnings", "Started At", "Completed At", "Log Output"])
    for agent in agent_states:
        writer.writerow([
            agent.agent_name,
            agent.status,
            str(agent.errors_found),
            agent.started_at.isoformat() if agent.started_at else "",
            agent.completed_at.isoformat() if agent.completed_at else "",
            (agent.log_output or "").replace("\n", " | "),
        ])

    output.seek(0)
    filename = f"{task.id}_test_report.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers=headers)

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    db.delete(task)
    db.commit()
    return {"message": f"Task {task_id} successfully deleted"}

@app.get("/api/screenshots/{task_id}/{filename}")
def get_screenshot(task_id: str, filename: str):
    safe_filename = os.path.basename(filename)
    candidate_dirs = [
        os.path.abspath(os.path.join("screenshots", task_id)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "screenshots", task_id)),
    ]

    for screenshot_dir in candidate_dirs:
        screenshot_path = os.path.abspath(os.path.join(screenshot_dir, safe_filename))
        if screenshot_path.startswith(screenshot_dir) and os.path.isfile(screenshot_path):
            return FileResponse(screenshot_path, media_type="image/png")

    raise HTTPException(status_code=404, detail="Screenshot not found")

@app.get("/api/dashboard/stats", response_model=schemas.DashboardStats)
def get_dashboard_stats(db: Session = Depends(get_db)):
    # Task Counts
    total_tasks = db.query(func.count(models.Task.id)).scalar() or 0
    total_test_cases = db.query(func.count(models.TestCase.id)).scalar() or 0
    total_errors = db.query(func.count(models.TestError.id)).scalar() or 0
    total_suggestions = db.query(func.count(models.Suggestion.id)).scalar() or 0
    
    # Calculate Success Rate
    passed_tests = db.query(func.count(models.TestCase.id)).filter(models.TestCase.status == "passed").scalar() or 0
    failed_tests = db.query(func.count(models.TestCase.id)).filter(models.TestCase.status == "failed").scalar() or 0
    total_run_tests = passed_tests + failed_tests
    
    success_rate = 100.0
    if total_run_tests > 0:
        success_rate = round((passed_tests / total_run_tests) * 100, 1)
        
    # Group tasks by status
    status_query = db.query(models.Task.status, func.count(models.Task.id)).group_by(models.Task.status).all()
    status_counts = {status: count for status, count in status_query}
    
    # Initialize missing statuses to zero for client predictability
    for status in ["pending", "crawling", "generating_test_cases", "running_tests", "completed", "failed"]:
        if status not in status_counts:
            status_counts[status] = 0
            
    return schemas.DashboardStats(
        total_tasks=total_tasks,
        total_test_cases=total_test_cases,
        total_errors=total_errors,
        total_suggestions=total_suggestions,
        success_rate=success_rate,
        status_counts=status_counts
    )
