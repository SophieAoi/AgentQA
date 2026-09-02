from unittest.mock import patch

import pytest

import agent.runner as runner
from agent.runner import TestCaseValidationError, delete_test_case, list_test_cases, load_test_case, save_test_case


def test_list_test_cases_returns_all_real_cases():
    cases = list_test_cases()
    ids = [c["id"] for c in cases]
    assert {"TC-001", "TC-002", "TC-003", "TC-004"}.issubset(ids)

    by_suite = {}
    for case in cases:
        by_suite.setdefault(case.get("suite"), []).append(case["id"])

    assert len(by_suite.get("Login", [])) == 14
    assert len(by_suite.get("Campaign Creation", [])) == 12
    assert len(by_suite.get("Line Item", [])) == 18
    assert len(by_suite.get("Deal Management", [])) == 14
    assert len(by_suite.get("Inventory", [])) == 9
    assert len(by_suite.get("Content Hub", [])) == 13
    assert len(by_suite.get("Delivery Reports", [])) == 7
    assert len(by_suite.get("Player Testing", [])) == 88
    assert len(by_suite.get("Planner", [])) == 13
    assert len(by_suite.get("Creative Types", [])) == 10
    assert len(by_suite.get("Day Parting", [])) == 46
    assert len(by_suite.get("Content Hub Extended", [])) == 11
    assert by_suite.get(None, []) == []  # TC-001..004 now tagged into Line Item
    assert len(ids) == sum(len(v) for v in by_suite.values())

    for case in cases:
        assert case["title"]
        assert case["description"]


def test_essential_cases_are_hand_picked_and_never_gapped():
    """
    `essential: true` is a hand-picked, honest "core happy-path or
    foundational guard rail" judgment call per suite — not a fabricated
    priority label (see TestCase.essential's docstring in
    app/models/schemas.py). Exactly 17 cases across 11 suites (Player
    Testing has zero runnable cases, so nothing there could be picked);
    every one of them must actually be runnable, since an essential
    "smoke test" selection that includes a guaranteed-to-fail gapped case
    would defeat the point.
    """
    cases = list_test_cases()
    essential = [c for c in cases if c.get("essential")]

    assert len(essential) == 17

    for case in essential:
        gapped = any(p.startswith("GAP:") for p in case.get("preconditions", []))
        assert not gapped, f"{case['id']} is marked essential but is gapped"

    essential_by_suite = {}
    for case in essential:
        essential_by_suite.setdefault(case.get("suite"), set()).add(case["id"])

    assert essential_by_suite == {
        "Login": {"AD_LG_01", "AD_LG_04"},
        "Campaign Creation": {"Ads_NC_15", "INF_PG_01"},
        "Line Item": {"TC-001", "INF_LI_02"},
        "Deal Management": {"Ads_DL_LT_25", "TC_POS_001"},
        "Inventory": {"Ads_INV_07", "Ads_ND_LT_308"},
        "Content Hub": {"Ads_CH_05"},
        "Content Hub Extended": {"TC_CH_004"},
        "Delivery Reports": {"Ads_ND_LT_405"},
        "Creative Types": {"TC_CR_004"},
        "Planner": {"TC_SPOT_002"},
        "Day Parting": {"TC-N-07", "TC-P-01"},
    }


def test_load_test_case_raises_for_unknown_id():
    import pytest

    with pytest.raises(ValueError, match="Unknown test case"):
        load_test_case("TC-999")


def test_get_test_cases_endpoint(client):
    response = client.get("/test-cases")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == len(list_test_cases())
    ids = [case["id"] for case in body]
    assert "TC-001" in ids
    assert "preconditions" in body[0]


# save_test_case() / delete_test_case() — the write path new/edit/delete
# depend on. Isolated from the real agent/test_cases/ directory via a
# tmp_path override of runner.TEST_CASES_DIR, so these tests can never
# create, edit, or delete a real test case file.


@pytest.fixture
def isolated_test_cases_dir(tmp_path):
    with patch.object(runner, "TEST_CASES_DIR", tmp_path):
        yield tmp_path


def test_save_test_case_creates_a_file_matching_house_style(isolated_test_cases_dir):
    save_test_case(
        "TC_NEW_001",
        "A new case",
        "Do the thing and check the result.",
        suite="Scratch",
        overwrite=False,
    )

    content = (isolated_test_cases_dir / "TC_NEW_001.yaml").read_text()
    assert content == (
        "id: TC_NEW_001\n"
        "title: A new case\n"
        "description: >\n"
        "  Do the thing and check the result.\n"
        "preconditions: []\n"
        "suite: Scratch\n"
    )


def test_save_test_case_create_rejects_a_duplicate_id(isolated_test_cases_dir):
    save_test_case("TC_DUP", "First", "First description.", overwrite=False)

    with pytest.raises(TestCaseValidationError, match="already exists"):
        save_test_case("TC_DUP", "Second", "Second description.", overwrite=False)


def test_save_test_case_edit_rejects_an_unknown_id(isolated_test_cases_dir):
    with pytest.raises(TestCaseValidationError, match="Unknown test case"):
        save_test_case("TC_GHOST", "Edited", "Edited description.", overwrite=True)


def test_save_test_case_edit_overwrites_the_existing_file(isolated_test_cases_dir):
    save_test_case("TC_EDIT_ME", "Original title", "Original description.", overwrite=False)
    save_test_case(
        "TC_EDIT_ME", "New title", "New description.", suite="Login", essential=True, overwrite=True
    )

    saved = load_test_case("TC_EDIT_ME")
    assert saved["title"] == "New title"
    assert saved["description"] == "New description.\n"
    assert saved["suite"] == "Login"
    assert saved["essential"] is True


def test_save_test_case_rejects_a_path_traversal_id(isolated_test_cases_dir):
    with pytest.raises(TestCaseValidationError, match="Invalid test case id"):
        save_test_case("../../etc/passwd", "t", "d", overwrite=False)

    # Confirms nothing was written outside the isolated directory.
    assert list(isolated_test_cases_dir.parent.glob("passwd")) == []


def test_save_test_case_rejects_an_id_with_unsafe_characters(isolated_test_cases_dir):
    for bad_id in ("has space", "has/slash", "has.dot", "semicolon;here", ""):
        with pytest.raises(TestCaseValidationError):
            save_test_case(bad_id, "t", "d", overwrite=False)


def test_save_test_case_requires_a_non_empty_title_and_description(isolated_test_cases_dir):
    with pytest.raises(TestCaseValidationError, match="title"):
        save_test_case("TC_BLANK", "", "A description.", overwrite=False)
    with pytest.raises(TestCaseValidationError, match="description"):
        save_test_case("TC_BLANK", "A title", "", overwrite=False)


def test_delete_test_case_removes_the_file(isolated_test_cases_dir):
    save_test_case("TC_TO_DELETE", "Title", "Description.", overwrite=False)
    assert (isolated_test_cases_dir / "TC_TO_DELETE.yaml").exists()

    delete_test_case("TC_TO_DELETE")

    assert not (isolated_test_cases_dir / "TC_TO_DELETE.yaml").exists()
    with pytest.raises(ValueError, match="Unknown test case"):
        load_test_case("TC_TO_DELETE")


def test_delete_test_case_raises_for_unknown_id(isolated_test_cases_dir):
    with pytest.raises(TestCaseValidationError, match="Unknown test case"):
        delete_test_case("TC_NEVER_EXISTED")


# Router-level tests (POST/PUT/DELETE /test-cases) — same isolation
# pattern so these never touch the real files either.


def test_create_test_case_endpoint(client, isolated_test_cases_dir):
    response = client.post(
        "/test-cases",
        json={
            "id": "TC_API_NEW",
            "title": "Created via API",
            "description": "Do the thing via the API.",
            "suite": "Scratch",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "TC_API_NEW"
    assert body["title"] == "Created via API"
    assert (isolated_test_cases_dir / "TC_API_NEW.yaml").exists()


def test_create_test_case_endpoint_rejects_duplicate_id(client, isolated_test_cases_dir):
    save_test_case("TC_API_DUP", "Existing", "Existing description.", overwrite=False)

    response = client.post(
        "/test-cases",
        json={"id": "TC_API_DUP", "title": "New", "description": "New description."},
    )
    assert response.status_code == 400


def test_update_test_case_endpoint(client, isolated_test_cases_dir):
    save_test_case("TC_API_EDIT", "Original", "Original description.", overwrite=False)

    response = client.put(
        "/test-cases/TC_API_EDIT",
        json={"title": "Updated", "description": "Updated description.", "essential": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Updated"
    assert body["essential"] is True


def test_update_test_case_endpoint_404s_for_unknown_id(client, isolated_test_cases_dir):
    response = client.put(
        "/test-cases/TC_API_GHOST",
        json={"title": "t", "description": "d"},
    )
    assert response.status_code == 404


def test_delete_test_case_endpoint(client, isolated_test_cases_dir):
    save_test_case("TC_API_DELETE", "Title", "Description.", overwrite=False)

    response = client.delete("/test-cases/TC_API_DELETE")
    assert response.status_code == 204
    assert not (isolated_test_cases_dir / "TC_API_DELETE.yaml").exists()


def test_delete_test_case_endpoint_404s_for_unknown_id(client, isolated_test_cases_dir):
    response = client.delete("/test-cases/TC_API_NEVER_EXISTED")
    assert response.status_code == 404


def test_write_endpoints_require_authentication(unauthenticated_client, isolated_test_cases_dir):
    assert unauthenticated_client.post(
        "/test-cases", json={"id": "TC_X", "title": "t", "description": "d"}
    ).status_code == 401
    assert unauthenticated_client.put(
        "/test-cases/TC_X", json={"title": "t", "description": "d"}
    ).status_code == 401
    assert unauthenticated_client.delete("/test-cases/TC_X").status_code == 401
