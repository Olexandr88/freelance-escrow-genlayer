"""
Executable lifecycle tests for FreelanceEscrow, using GenLayer's
Direct Mode (`genlayer-test` / `gltest`): the contract runs in-memory,
no GenLayer Studio or Docker required.

Setup:
    pip install genlayer-test

Run (from the repository root):
    gltest tests/ -v
"""

import json

import pytest

CONTRACT_PATH = "contracts/freelance_escrow.py"
WORK_URL = "https://example.com/work"

CRITERIA = json.dumps(
    [
        {"name": "README", "weight": 20, "description": "Project has a clear README"},
        {"name": "Tests", "weight": 50, "description": "Automated tests are included"},
        {"name": "Demo", "weight": 30, "description": "A working demo link is provided"},
    ]
)


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy(CONTRACT_PATH)


def _create(contract, direct_vm, client, freelancer, amount=1000):
    """Create a job as `client` and return its id (ids are sequential from 1)."""
    direct_vm.sender = client
    contract.create_job(freelancer, "Landing page", CRITERIA, amount)
    return int(contract.total_jobs())


def _submit(contract, direct_vm, freelancer, job_id):
    direct_vm.sender = freelancer
    contract.submit_work(job_id, WORK_URL)


def _arbitrate(contract, direct_vm, sender, job_id, action, payout_pct):
    """
    Run `dispute` or `appeal` with deterministic web and LLM mocks.
    The mocked LLM returns a fixed verdict with the given payout percentage.
    """
    direct_vm.mock_web(
        r".*",
        {"status": 200, "body": "Delivered work: README, automated tests and a demo link."},
    )
    direct_vm.mock_llm(
        r".*",
        json.dumps(
            {
                "scores": [
                    {"name": "README", "score": 1.0},
                    {"name": "Tests", "score": 0.5},
                    {"name": "Demo", "score": 1.0},
                ],
                "payout_pct": payout_pct,
                "reasoning": "Mocked verdict.",
            }
        ),
    )
    direct_vm.sender = sender
    try:
        getattr(contract, action)(job_id)
    finally:
        direct_vm.clear_mocks()


class TestHappyPath:
    def test_client_approval_pays_full_amount(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        assert contract.get_job(job_id)["status"] == "CREATED"

        _submit(contract, direct_vm, direct_bob, job_id)
        assert contract.get_job(job_id)["status"] == "SUBMITTED"

        direct_vm.sender = direct_alice
        contract.approve(job_id)

        job = contract.get_job(job_id)
        assert job["status"] == "RELEASED"
        assert job["freelancer_pct"] == 100
        assert job["freelancer_amount"] == 1000


class TestDisputeArbitration:
    def test_dispute_returns_partial_payout(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        _arbitrate(contract, direct_vm, direct_alice, job_id, "dispute", 70)

        job = contract.get_job(job_id)
        assert job["status"] == "RESOLVED"
        assert job["freelancer_pct"] == 70
        assert job["freelancer_amount"] == 700
        assert job["verdict"]["payout_pct"] == 70

    def test_payout_is_clamped_and_rounded_to_step(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        # 137 must be clamped to 100 by the contract.
        _arbitrate(contract, direct_vm, direct_bob, job_id, "dispute", 137)

        assert contract.get_job(job_id)["freelancer_pct"] == 100


class TestAppeal:
    def test_single_appeal_can_change_outcome_then_final(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        _arbitrate(contract, direct_vm, direct_alice, job_id, "dispute", 50)
        assert contract.get_job(job_id)["freelancer_pct"] == 50

        _arbitrate(contract, direct_vm, direct_bob, job_id, "appeal", 90)

        job = contract.get_job(job_id)
        assert job["status"] == "FINAL"
        assert job["appealed"] is True
        assert job["freelancer_pct"] == 90

        with direct_vm.expect_revert("appeal not available"):
            _arbitrate(contract, direct_vm, direct_bob, job_id, "appeal", 10)

    def test_appeal_before_dispute_reverts(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("appeal not available"):
            contract.appeal(job_id)


class TestAccessControlAndValidation:
    def test_client_cannot_submit_work(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("only freelancer can submit"):
            contract.submit_work(job_id, WORK_URL)

    def test_freelancer_cannot_approve_own_work(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)
        with direct_vm.expect_revert("only client can approve"):
            contract.approve(job_id)

    def test_dispute_requires_submitted_work(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("job not in SUBMITTED state"):
            contract.dispute(job_id)

    def test_criteria_weights_must_sum_to_100(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        bad = json.dumps(
            [{"name": "Only", "weight": 60, "description": "weights do not sum to 100"}]
        )
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("criteria weights must sum to 100"):
            contract.create_job(direct_bob, "Bad job", bad, 1000)

    def test_unknown_job_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("job not found"):
            contract.get_job(999)
