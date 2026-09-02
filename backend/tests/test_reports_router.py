from app.models.schemas import TestStepResult


def test_get_report_html_404s_for_unknown_run(client):
    response = client.get("/test-runs/does-not-exist/report")
    assert response.status_code == 404


def test_get_report_pdf_404s_for_unknown_run(client):
    response = client.get("/test-runs/does-not-exist/report.pdf")
    assert response.status_code == 404


def test_get_report_html_renders_for_known_run(client, store):
    run = store.create_test_run(["TC-001"])
    store.update_test_run(
        run.run_id,
        status="passed",
        passed_count=1,
        total_count=1,
        steps=[TestStepResult(step_description="Log in", status="OK")],
    )

    response = client.get(f"/test-runs/{run.run_id}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert run.run_id in response.text
    assert "Log in" in response.text


def test_get_report_pdf_renders_for_known_run(client, store):
    run = store.create_test_run(["TC-001"])
    store.update_test_run(
        run.run_id,
        status="passed",
        passed_count=1,
        total_count=1,
        steps=[TestStepResult(step_description="Log in", status="OK")],
    )

    response = client.get(f"/test-runs/{run.run_id}/report.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"
