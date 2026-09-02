"""
Report endpoints — human-consumable HTML/PDF views over the structured
TestRunDetail/ExecutionStep data (docs/phase-06-reporting-and-chat-trigger.md).
"""

from fastapi import APIRouter, Depends, HTTPException, Response

from agent.reporter import render_html, render_pdf
from app.routers.auth import get_current_user
from app.services.store import StoreProtocol, get_store

router = APIRouter(prefix="/test-runs", tags=["reports"], dependencies=[Depends(get_current_user)])


@router.get("/{run_id}/report", response_class=Response)
async def get_report_html(run_id: str, store: StoreProtocol = Depends(get_store)):
    run = store.get_test_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")
    html = render_html(run, store.get_execution_steps(run_id))
    return Response(content=html, media_type="text/html")


@router.get("/{run_id}/report.pdf", response_class=Response)
async def get_report_pdf(run_id: str, store: StoreProtocol = Depends(get_store)):
    run = store.get_test_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")
    html = render_html(run, store.get_execution_steps(run_id))
    pdf_bytes = await render_pdf(html)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{run_id}.pdf"'},
    )
