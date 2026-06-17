import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Float, Integer
from sqlalchemy.orm import relationship
from database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=generate_uuid)
    url = Column(String, nullable=False)
    status = Column(String, default="pending") # pending, crawling, generating_test_cases, running_tests, completed, failed
    is_mobile = Column(Integer, default=0) # 0 = desktop, 1 = mobile
    ai_model = Column(String, default="gemini-1.5-flash")
    user_prompt = Column(Text, nullable=True)
    custom_use_cases_json = Column(Text, nullable=True)
    page_snapshots_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    use_cases = relationship("UseCase", back_populates="task", cascade="all, delete-orphan")
    test_cases = relationship("TestCase", back_populates="task", cascade="all, delete-orphan")
    errors = relationship("TestError", back_populates="task", cascade="all, delete-orphan")
    suggestions = relationship("Suggestion", back_populates="task", cascade="all, delete-orphan")
    codebase = relationship("Codebase", back_populates="task", uselist=False, cascade="all, delete-orphan")
    auth = relationship("TaskAuth", back_populates="task", uselist=False, cascade="all, delete-orphan")
    seeds = relationship("TaskSeed", back_populates="task", uselist=False, cascade="all, delete-orphan")
    agent_states = relationship("AgentState", back_populates="task", cascade="all, delete-orphan")


class UseCase(Base):
    __tablename__ = "use_cases"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="use_cases")
    test_cases = relationship("TestCase", back_populates="use_case", cascade="all, delete-orphan")


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    use_case_id = Column(String, ForeignKey("use_cases.id", ondelete="CASCADE"), nullable=True)
    title = Column(String, nullable=False)
    steps = Column(Text, nullable=True) # newline separated steps
    expected_result = Column(Text, nullable=True)
    status = Column(String, default="pending") # pending, passed, failed
    error_message = Column(Text, nullable=True)
    execution_time = Column(Float, nullable=True) # in seconds
    page_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="test_cases")
    use_case = relationship("UseCase", back_populates="test_cases")
    errors = relationship("TestError", back_populates="test_case")


class TestError(Base):
    __tablename__ = "test_errors"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    test_case_id = Column(String, ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=False)
    severity = Column(String, default="medium") # low, medium, high, critical
    page_url = Column(String, nullable=False)
    screenshot_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="errors")
    test_case = relationship("TestCase", back_populates="errors")
    code_reference = relationship("CodeReference", back_populates="test_error", uselist=False, cascade="all, delete-orphan")


class Suggestion(Base):
    __tablename__ = "suggestions"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String, default="medium") # low, medium, high
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="suggestions")


class Codebase(Base):
    __tablename__ = "codebases"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    local_path = Column(String, nullable=False)
    framework_type = Column(String, nullable=False, default="React")
    file_tree = Column(Text, nullable=True) # JSON serialized file tree
    analyzed_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="codebase")


class TaskAuth(Base):
    __tablename__ = "task_auths"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True)
    auth_required = Column(Integer, default=0)
    auth_login_url = Column(String, nullable=True)
    auth_post_login_url = Column(String, nullable=True)
    auth_username = Column(String, nullable=True)
    auth_password = Column(String, nullable=True)
    auth_otp_code = Column(String, nullable=True)
    auth_otp_hint = Column(String, nullable=True)
    auth_flow = Column(String, nullable=True)
    auth_next_step = Column(String, nullable=True)
    auth_required_fields = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="auth")


class TaskSeed(Base):
    __tablename__ = "task_seeds"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True)
    seed_urls_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="seeds")


class AgentState(Base):
    __tablename__ = "agent_states"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    agent_name = Column(String, nullable=False) # UI_UX, Responsive, Form, API, Image, CodeReview
    status = Column(String, default="pending") # pending, running, completed, failed
    errors_found = Column(Integer, default=0)
    log_output = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    task = relationship("Task", back_populates="agent_states")


class CodeReference(Base):
    __tablename__ = "code_references"

    id = Column(String, primary_key=True, default=generate_uuid)
    test_error_id = Column(String, ForeignKey("test_errors.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String, nullable=False)
    start_line = Column(Integer, nullable=False)
    end_line = Column(Integer, nullable=False)
    code_snippet = Column(Text, nullable=False)
    proposed_fix = Column(Text, nullable=True)

    test_error = relationship("TestError", back_populates="code_reference")
