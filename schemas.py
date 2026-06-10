from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional, Dict

class TaskBase(BaseModel):
    url: str

class TaskCreate(TaskBase):
    codebase_path: Optional[str] = None
    seed_urls: Optional[List[str]] = None
    auth_required: Optional[bool] = False
    auth_login_url: Optional[str] = None
    auth_post_login_url: Optional[str] = None
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_otp_code: Optional[str] = None
    auth_otp_hint: Optional[str] = None

class TaskInputUpdate(BaseModel):
    auth_required: Optional[bool] = None
    auth_login_url: Optional[str] = None
    auth_post_login_url: Optional[str] = None
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_otp_code: Optional[str] = None
    auth_otp_hint: Optional[str] = None
    form_values_json: Optional[str] = None
    seed_urls: Optional[List[str]] = None

class TaskResponse(BaseModel):
    id: str
    url: str
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    
    # Nested counts
    use_case_count: int = 0
    test_case_count: int = 0
    error_count: int = 0
    suggestion_count: int = 0

    class Config:
        from_attributes = True

class UseCaseResponse(BaseModel):
    id: str
    task_id: str
    title: str
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class TestCaseResponse(BaseModel):
    id: str
    task_id: str
    use_case_id: Optional[str] = None
    title: str
    steps: Optional[str] = None
    expected_result: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    execution_time: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True

class CodebaseResponse(BaseModel):
    id: str
    task_id: str
    local_path: str
    framework_type: str
    file_tree: Optional[str] = None
    analyzed_at: datetime

    class Config:
        from_attributes = True

class TaskAuthResponse(BaseModel):
    id: str
    task_id: str
    auth_required: bool
    auth_login_url: Optional[str] = None
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_otp_code: Optional[str] = None
    auth_otp_hint: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class TaskSeedResponse(BaseModel):
    id: str
    task_id: str
    seed_urls_json: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class AgentStateResponse(BaseModel):
    id: str
    task_id: str
    agent_name: str
    status: str
    errors_found: int
    log_output: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CodeReferenceResponse(BaseModel):
    id: str
    test_error_id: str
    file_path: str
    start_line: int
    end_line: int
    code_snippet: str
    proposed_fix: Optional[str] = None

    class Config:
        from_attributes = True

class TestErrorResponse(BaseModel):
    id: str
    task_id: str
    test_case_id: Optional[str] = None
    message: str
    severity: str
    page_url: str
    screenshot_path: Optional[str] = None
    created_at: datetime
    code_reference: Optional[CodeReferenceResponse] = None

    class Config:
        from_attributes = True

class SuggestionResponse(BaseModel):
    id: str
    task_id: str
    title: str
    description: Optional[str] = None
    priority: str
    created_at: datetime

    class Config:
        from_attributes = True

class TaskDetailsResponse(BaseModel):
    task: TaskResponse
    use_cases: List[UseCaseResponse]
    test_cases: List[TestCaseResponse]
    errors: List[TestErrorResponse]
    suggestions: List[SuggestionResponse]
    codebase: Optional[CodebaseResponse] = None
    auth: Optional[TaskAuthResponse] = None
    seeds: Optional[TaskSeedResponse] = None
    agent_states: List[AgentStateResponse] = []

class DashboardStats(BaseModel):
    total_tasks: int
    total_test_cases: int
    total_errors: int
    total_suggestions: int
    success_rate: float # percentage of passed test cases
    status_counts: Dict[str, int] # e.g. {"completed": 5, "running": 1}
