import sys
import os
import json
import logging
import traceback
from datetime import datetime

# Set up paths so it can import local modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SessionLocal
from models import Task, TaskAuth, TaskSeed, AgentState, UseCase, TestCase, TestError, Suggestion
from agent import run_testing_agent, run_test_execution_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_sellingo_validation")

def main():
    logger.info("Initializing Sellingo AI Validation Run...")
    db = SessionLocal()
    try:
        # 1. Create the Task
        task = Task(
            url="https://sellingo.ai",
            status="pending",
            is_mobile=0,
            ai_model="gemini-1.5-flash",
            user_prompt="Verify catalog management features: adding, editing, and deleting items. Check custom order pages, form fillings, and modals."
        )
        db.add(task)
        db.flush()
        logger.info(f"Created Task ID: {task.id}")

        # 2. Create the TaskAuth
        auth = TaskAuth(
            task_id=task.id,
            auth_required=1,
            auth_login_url="https://sellingo.ai",
            auth_post_login_url="https://sellingo.ai/merchant/mycatalog",
            auth_username="8983587710",
            auth_password="999999"
        )
        db.add(auth)

        # 3. Create the TaskSeed
        seed = TaskSeed(
            task_id=task.id,
            seed_urls_json=json.dumps(["https://sellingo.ai/merchant/mycatalog"])
        )
        db.add(seed)

        # 4. Create AgentState records
        for agent_name in ["Orchestrator", "UI_UX", "Responsive", "Form", "API", "Image", "CodeReview"]:
            state = AgentState(
                task_id=task.id,
                agent_name=agent_name,
                status="pending"
            )
            db.add(state)

        db.commit()
        logger.info("Database initialized successfully. Running Planning Stage (crawling + test planning)...")

        # 5. Run the planning agent (run_testing_agent)
        run_testing_agent(task.id)
        
        # Refresh task
        db.refresh(task)
        logger.info(f"Planning Stage Complete. Task Status: {task.status}")

        # Fetch planning state log
        orchestrator_state = db.query(AgentState).filter(
            AgentState.task_id == task.id, 
            AgentState.agent_name == "Orchestrator"
        ).first()
        if orchestrator_state:
            logger.info("=== ORCHESTRATOR PLANNING LOG ===")
            logger.info(orchestrator_state.log_output or "No log output")
            logger.info("=================================")

        if task.status != "planned":
            logger.error("Planning stage did not finish with status 'planned'. Aborting execution.")
            return

        # Let's inspect the generated Use Cases and Test Cases
        use_cases = db.query(UseCase).filter(UseCase.task_id == task.id).all()
        logger.info(f"Generated {len(use_cases)} Use Cases:")
        for uc in use_cases:
            tcs = db.query(TestCase).filter(TestCase.use_case_id == uc.id).all()
            logger.info(f"  UseCase: {uc.title} ({len(tcs)} test cases)")
            for tc in tcs:
                logger.info(f"    TestCase: {tc.title} (check_type not directly in DB, but title gives clue)")

        # 6. Run the execution agent (run_test_execution_agent)
        logger.info("Running Test Execution Stage (browser tests)...")
        run_test_execution_agent(task.id)

        # Refresh task and fetch final results
        db.refresh(task)
        logger.info(f"Execution Stage Complete. Task Status: {task.status}")

        # Fetch execution log
        orchestrator_state = db.query(AgentState).filter(
            AgentState.task_id == task.id, 
            AgentState.agent_name == "Orchestrator"
        ).first()
        if orchestrator_state:
            logger.info("=== ORCHESTRATOR EXECUTION LOG ===")
            logger.info(orchestrator_state.log_output or "No log output")
            logger.info("==================================")

        # Print detailed execution results of test cases
        logger.info("=== TEST CASES RESULTS ===")
        test_cases = db.query(TestCase).filter(TestCase.task_id == task.id).all()
        passed_count = 0
        failed_count = 0
        pending_count = 0
        for tc in test_cases:
            if tc.status == "passed":
                passed_count += 1
            elif tc.status == "failed":
                failed_count += 1
            else:
                pending_count += 1
            logger.info(f"[{tc.status.upper()}] {tc.title} on {tc.page_url}")
            if tc.error_message:
                logger.info(f"      Error: {tc.error_message}")

        logger.info(f"Summary: Total: {len(test_cases)}, Passed: {passed_count}, Failed: {failed_count}, Pending: {pending_count}")

        # Print errors
        errors = db.query(TestError).filter(TestError.task_id == task.id).all()
        logger.info(f"Captured {len(errors)} errors:")
        for err in errors:
            logger.info(f"  [{err.severity.upper()}] {err.message} on {err.page_url}")

    except Exception as e:
        logger.error(f"Error during validation: {e}")
        logger.error(traceback.format_exc())
    finally:
        db.close()

if __name__ == "__main__":
    main()
