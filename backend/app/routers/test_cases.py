"""
Serves the real test case definitions from agent/test_cases/*.yaml, replacing
the hardcoded AVAILABLE_TEST_CASES array that used to live in
TestRunnerPanel.jsx.

Create/edit/delete below write straight through to those same YAML files —
there's no separate database copy to keep in sync, so a change made here is
immediately what list_test_cases()/load_test_case() see on the very next
read (including the next test run).
"""

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import TestCase, TestCaseCreate, TestCaseWrite
from app.routers.auth import get_current_user
from agent.runner import TestCaseValidationError, delete_test_case, list_test_cases, save_test_case

router = APIRouter(prefix="/test-cases", tags=["test-cases"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[TestCase])
def get_test_cases():
    return list_test_cases()


@router.post("", response_model=TestCase, status_code=201)
def create_test_case(body: TestCaseCreate):
    try:
        return save_test_case(
            body.id,
            body.title,
            body.description,
            suite=body.suite,
            preconditions=body.preconditions,
            essential=body.essential,
            overwrite=False,
        )
    except TestCaseValidationError as exc:
        # A validation error here is always the caller's fault (bad id,
        # missing field, id collision) — 400, never a 500, so the
        # frontend can show the exact message rather than a generic
        # "something went wrong."
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{test_case_id}", response_model=TestCase)
def update_test_case(test_case_id: str, body: TestCaseWrite):
    try:
        return save_test_case(
            test_case_id,
            body.title,
            body.description,
            suite=body.suite,
            preconditions=body.preconditions,
            essential=body.essential,
            overwrite=True,
        )
    except TestCaseValidationError as exc:
        status = 404 if "Unknown test case" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/{test_case_id}", status_code=204)
def remove_test_case(test_case_id: str):
    try:
        delete_test_case(test_case_id)
    except TestCaseValidationError as exc:
        status = 404 if "Unknown test case" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
