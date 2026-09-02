"""
Test run endpoints — lets the frontend trigger the agent to run a set
of test cases end-to-end, and poll for progress/results.

Runs happen in the background (FastAPI's BackgroundTasks) since
browser automation takes real time — the POST returns immediately
with a run_id, and the frontend polls GET /test-runs/{run_id}.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.models.schemas import AgentTrace, TestRunDetail, TestRunRequest, TestRunSummary
from app.routers.auth import get_current_user
from app.services.agent_runner import run_test_suite
from app.services.event_bus import EventBus, get_event_bus
from app.services.store import StoreProtocol, get_store

router = APIRouter(prefix="/test-runs", tags=["test-runs"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=TestRunSummary)
async def start_test_run(
    request: TestRunRequest,
    background_tasks: BackgroundTasks,
    store: StoreProtocol = Depends(get_store),
    event_bus: EventBus = Depends(get_event_bus),
):
    if not request.test_case_ids:
        raise HTTPException(status_code=400, detail="test_case_ids cannot be empty")

    run = store.create_test_run(request.test_case_ids)
    background_tasks.add_task(run_test_suite, store, event_bus, run.run_id, request.test_case_ids)
    return run


@router.get("/{run_id}", response_model=TestRunDetail)
async def get_test_run(run_id: str, store: StoreProtocol = Depends(get_store)):
    run = store.get_test_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")
    return run


@router.get("", response_model=list[TestRunSummary])
async def list_test_runs(store: StoreProtocol = Depends(get_store)):
    return store.list_test_runs()


@router.get("/{run_id}/trace", response_model=list[AgentTrace])
async def get_test_run_trace(run_id: str, store: StoreProtocol = Depends(get_store)):
    """
    Full Planner/Executor reasoning and tool-call trace for a run — debugging
    detail, not surfaced in the main polling UI (see docs/BUILD-PLAN.md).
    """
    if not store.get_test_run(run_id):
        raise HTTPException(status_code=404, detail="Test run not found")
    return store.get_agent_traces(run_id)
