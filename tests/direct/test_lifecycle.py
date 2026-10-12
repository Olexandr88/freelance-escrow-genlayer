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

# Weights: README 20, Tests 50, Demo 30 (sum 100)
CRITERIA = json.dumps(
    [
        {"name": "README", "weight": 20, "description": "Project has a clear README"},
        {"name": "Tests", "weight": 50, "description": "Automated tests are included"},
        {"name": "Demo", "weight": 30, "description": "A working demo link is provided"},
    ]
)

# Per-criterion scores (README, Tests, Demo) and the payout they produce:
#   20*r + 50*t + 30*d
SCORES_50 = (100, 0, 100)
SCORES_70 = (100, 40, 100)
SCORES_90 = (100, 80, 100)
SCORES_OVER = (200, 200, 200)


@pytest.fixture
def contract(direct_deploy, direct_vm):
    deployed = direct_deploy(CONTRACT_PATH)
    direct_vm.value = 0
    return deployed


def _create(contract, direct_vm, client, freelancer, amount=1000):
    """Create a funded job as `client` and return its id (ids start at 0)."""
    direct_vm.sender = client
    direct_vm.value = amount
    try:
        contract.create_job(freelancer, "Landing page", CRITERIA)
    finally:
        direct_vm.value = 0
    return int(contract.total_jobs()) - 1


def _submit(contract, direct_vm, freelancer, job_id):
    direct_vm.sender = freelancer
    contract.submit_work(job_id, WORK_URL)


def _arbitrate(contract, direct_vm, sender, job_id, action, scores):
    """
    Run `dispute` or `appeal` with deterministic web and LLM mocks.
    The mocked LLM returns per-criterion scores; the contract itself
    computes the payout percentage from the weights.
    """
    readme, tests, demo = scores
    direct_vm.mock_web(
        r".*",
        {
            "status": 200,
            "body": "Delivered work: README, automated tests and a live demo link.",
        },
    )
    direct_vm.mock_llm(
        r".*",
        json.dumps(
            {
                "scores": [
                    {"name": "README", "score": readme},
                    {"name": "Tests", "score": tests},
                    {"name": "Demo", "score": demo},
                ],
                "reasoning": "Mocked evidence supports the scores above.",
            }
        ),
    )
    direct_vm.sender = sender
    try:
        getattr(contract, action)(job_id)
    finally:
        direct_vm.clear_mocks()


class TestFundingAndHappyPath:
    def test_create_job_locks_escrow(self, contract, direct_vm, direct_alice, direct_bob):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob, 1000)

        job = contract.get_job(job_id)
        assert job["status"] == "CREATED"
        assert job["amount"] == 1000
        assert contract.get_escrow_balance() == 1000

    def test_client_approval_pays_full_amount(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)
        assert contract.get_job(job_id)["status"] == "SUBMITTED"

        direct_vm.sender = direct_alice
        contract.approve(job_id)

        job = contract.get_job(job_id)
        assert job["status"] == "RELEASED"
        assert job["freelancer_pct"] == 100
        assert job["freelancer_amount"] == 1000
        assert job["paid"] is True
        assert contract.get_escrow_balance() == 0

    def test_approve_cannot_be_repeated(self, contract, direct_vm, direct_alice, direct_bob):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)
        direct_vm.sender = direct_alice
        contract.approve(job_id)

        with direct_vm.expect_revert("Nothing to approve"):
            contract.approve(job_id)


class TestDisputeArbitration:
    def test_dispute_computes_partial_payout_from_weights(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        _arbitrate(contract, direct_vm, direct_alice, job_id, "dispute", SCORES_70)

        job = contract.get_job(job_id)
        assert job["status"] == "RESOLVED"
        assert job["freelancer_pct"] == 70
        assert job["freelancer_amount"] == 700
        assert job["verdict"]["payout_pct"] == 70
        assert job["paid"] is False
        assert contract.get_escrow_balance() == 1000  # still locked until settle/appeal

    def test_settle_accepts_verdict_and_pays_out(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)
        _arbitrate(contract, direct_vm, direct_bob, job_id, "dispute", SCORES_70)

        direct_vm.sender = direct_alice
        contract.settle(job_id)

        job = contract.get_job(job_id)
        assert job["status"] == "FINAL"
        assert job["paid"] is True
        assert contract.get_escrow_balance() == 0

        # A settled job cannot be appealed afterwards
        with direct_vm.expect_revert("Appeal not available"):
            contract.appeal(job_id)

    def test_scores_are_clamped_to_full_payout(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        _arbitrate(contract, direct_vm, direct_bob, job_id, "dispute", SCORES_OVER)

        assert contract.get_job(job_id)["freelancer_pct"] == 100


class TestAppeal:
    def test_single_appeal_can_change_outcome_and_pays_out(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        _arbitrate(contract, direct_vm, direct_alice, job_id, "dispute", SCORES_50)
        assert contract.get_job(job_id)["freelancer_pct"] == 50

        _arbitrate(contract, direct_vm, direct_bob, job_id, "appeal", SCORES_90)

        job = contract.get_job(job_id)
        assert job["status"] == "FINAL"
        assert job["appealed"] is True
        assert job["freelancer_pct"] == 90
        assert job["freelancer_amount"] == 900
        assert job["paid"] is True
        assert contract.get_escrow_balance() == 0

        with direct_vm.expect_revert("Appeal not available"):
            _arbitrate(contract, direct_vm, direct_bob, job_id, "appeal", SCORES_50)

    def test_appeal_before_dispute_reverts(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)

        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Appeal not available"):
            contract.appeal(job_id)


class TestAccessControlAndValidation:
    def test_client_cannot_submit_work(self, contract, direct_vm, direct_alice, direct_bob):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Only the freelancer can submit work"):
            contract.submit_work(job_id, WORK_URL)

    def test_freelancer_cannot_approve_own_work(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        _submit(contract, direct_vm, direct_bob, job_id)
        with direct_vm.expect_revert("Only the client can approve"):
            contract.approve(job_id)

    def test_dispute_requires_submitted_work(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        job_id = _create(contract, direct_vm, direct_alice, direct_bob)
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Job is not in SUBMITTED state"):
            contract.dispute(job_id)

    def test_unfunded_job_reverts(self, contract, direct_vm, direct_alice, direct_bob):
        direct_vm.sender = direct_alice
        direct_vm.value = 0
        with direct_vm.expect_revert("Send some GEN"):
            contract.create_job(direct_bob, "Unfunded", CRITERIA)

    def test_criteria_weights_must_sum_to_100(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        bad = json.dumps(
            [{"name": "Only", "weight": 60, "description": "weights do not sum to 100"}]
        )
        direct_vm.sender = direct_alice
        direct_vm.value = 1000
        try:
            with direct_vm.expect_revert("criteria weights must sum to 100"):
                contract.create_job(direct_bob, "Bad job", bad)
        finally:
            direct_vm.value = 0

    def test_unknown_job_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Unknown job id"):
            contract.get_job(999)
