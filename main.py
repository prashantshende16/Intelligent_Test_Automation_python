import threading
import os
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List

from database import engine, Base, get_db
import models
import schemas
from agent import run_testing_agent

# Initialize Database tables
Base.metadata.create_all(bind=engine)

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
    thread = threading.Thread(target=run_testing_agent, args=(db_task.id,))
    thread.daemon = True
    thread.start()
    
    return get_task_with_counts(db_task, db)

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
    agent_states = db.query(models.AgentState).filter(models.AgentState.task_id == task_id).order_by(models.AgentState.started_at.asc()).all()
    
    return schemas.TaskDetailsResponse(
        task=task_resp,
        use_cases=use_cases,
        test_cases=test_cases,
        errors=errors,
        suggestions=suggestions,
        codebase=codebase,
        agent_states=agent_states
    )

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
