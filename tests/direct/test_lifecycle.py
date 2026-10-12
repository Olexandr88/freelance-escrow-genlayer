"""
Direct-mode lifecycle tests for FreelanceEscrow.

Scenarios:
  1. Happy path: freelancer submits, client approves, 100% payout.
  2. Dispute: AI arbitration returns a partial payout (70%).
  3. Appeal: one appeal is allowed, a second one is rejected.
  4. Access control and input validation.
"""
import json

CONTRACT = "contracts/freelance_escrow.py"
WORK_URL = "https://example.com/work"

CRITERIA = json.dumps([
    {"name": "README", "weight": 20, "description": "Project has a clear README"},
    {"name": "Tests", "weight": 50, "description": "Automated tests are included"},
    {"name": "Demo", "weight": 30, "description": "A working demo link is provided"},
])


def verdict(pct: int) -> str:
    return json.dumps({"scores": [], "payout_pct": pct, "reasoning": "mocked verdict"})


def setup_job(direct_vm, direct_deploy, client, freelancer):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = client
    job_id = contract.create_job(freelancer.as_hex, "Landing page", CRITERIA, 1000)
    return contract, job_id


def mock_work_page(direct_vm):
    direct_vm.mock_web(
        r"example\.com/work",
        {"method": "GET", "response": {"status": 200, "body": "README, tests and demo link"}},
    )


def test_happy_path_full_payout(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract, job_id = setup_job(direct_vm, direct_deploy, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.submit_work(job_id, WORK_URL)
    assert contract.get_job(job_id)["status"] == "SUBMITTED"

    direct_vm.sender = direct_alice
    contract.approve(job_id)

    job = contract.get_job(job_id)
    assert job["status"] == "RELEASED"
    assert job["freelancer_pct"] == 100
    assert job["freelancer_amount"] == 1000


def test_dispute_partial_payout(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract, job_id = setup_job(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    contract.submit_work(job_id, WORK_URL)

    mock_work_page(direct_vm)
    direct_vm.mock_llm(r".*neutral arbiter.*", verdict(70))

    direct_vm.sender = direct_alice
    contract.dispute(job_id)

    job = contract.get_job(job_id)
    assert job["status"] == "RESOLVED"
    assert job["freelancer_pct"] == 70
    assert job["freelancer_amount"] == 700
    assert job["verdict"]["payout_pct"] == 70


def test_single_appeal_then_final(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract, job_id = setup_job(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    contract.submit_work(job_id, WORK_URL)

    mock_work_page(direct_vm)
    direct_vm.mock_llm(r".*neutral arbiter.*", verdict(50))
    direct_vm.sender = direct_alice
    contract.dispute(job_id)
    assert contract.get_job(job_id)["freelancer_pct"] == 50

    # The appeal is re-evaluated and may change the outcome
    direct_vm.mock_llm(r".*neutral arbiter.*", verdict(90))
    direct_vm.sender = direct_bob
    contract.appeal(job_id)

    job = contract.get_job(job_id)
    assert job["status"] == "FINAL"
    assert job["appealed"] is True
    assert job["freelancer_pct"] == 90

    # A second appeal must be rejected
    with direct_vm.expect_revert("appeal not available"):
        contract.appeal(job_id)


def test_access_control(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract, job_id = setup_job(direct_vm, direct_deploy, direct_alice, direct_bob)

    # The client cannot submit work
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("only freelancer can submit"):
        contract.submit_work(job_id, WORK_URL)

    direct_vm.sender = direct_bob
    contract.submit_work(job_id, WORK_URL)

    # The freelancer cannot approve their own work
    with direct_vm.expect_revert("only client can approve"):
        contract.approve(job_id)


def test_invalid_criteria_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    bad = json.dumps([{"name": "Only", "weight": 60, "description": "weights do not sum to 100"}])
    with direct_vm.expect_revert("criteria weights must sum to 100"):
        contract.create_job(direct_bob.as_hex, "Bad job", bad, 1000)
