# freelance-escrow-genlayer

A freelance escrow **Intelligent Contract** for [GenLayer](https://www.genlayer.com/) with AI arbitration.

A client locks GEN in escrow against weighted acceptance criteria. A freelancer submits a deliverable URL. If the client approves, the freelancer is paid in full. If they disagree, GenLayer validators read the deliverable, score every criterion, and the contract computes the payout split deterministically from the criteria weights. The verdict can be appealed once.

This repository is contract-focused: the contract, its tests and a pinned test setup. There is no frontend.

## Lifecycle

    create_job (client locks GEN)  ->  CREATED
    submit_work (freelancer)       ->  SUBMITTED
    approve (client)               ->  RELEASED  (100% to the freelancer)
    dispute (either party)         ->  RESOLVED  (AI verdict)
    settle (either party)          ->  FINAL     (verdict paid out)
    appeal (either party, once)    ->  FINAL     (re-evaluated, paid out)

| State | Meaning |
|---|---|
| `CREATED` | Job created and funded, freelancer assigned |
| `SUBMITTED` | Freelancer submitted a deliverable URL |
| `RELEASED` | Client approved, 100% paid to the freelancer |
| `RESOLVED` | AI arbitration issued a verdict, waiting for settle or appeal |
| `FINAL` | Verdict accepted or re-evaluated after the single appeal, escrow paid out |

## How arbitration works

1. The client defines acceptance criteria as JSON. Weights must sum to 100, for example: `[{"name": "Tests", "weight": 50, "description": "Automated tests are included"}]`.
2. On `dispute`, validators fetch the deliverable URL and ask an LLM to give each criterion an **integer score from 0 to 100**, based only on evidence in the delivered work.
3. The contract, not the LLM, computes the payout: `payout_pct = round(sum(weight * score / 100) / 10) * 10`, clamped to 0-100. Missing criteria count as 0.
4. Consensus: the leader's `payout_pct` must follow from its own scores and the weights, and each validator accepts it if its own independent evaluation is within 10 points.
5. An empty or unreachable deliverable is never sent to the LLM; it scores 0 deterministically.
6. Scores are integers on purpose: floats are not calldata-encodable between validators.

On payout, the escrow is split on-chain: `payout_pct` percent to the freelancer, the remainder refunded to the client.

## Contract API

| Method | Who | Description |
|---|---|---|
| `create_job(freelancer, title, criteria_json)` (payable) | client | Locks the attached GEN as escrow |
| `submit_work(job_id, url)` | freelancer | Submits an http(s) deliverable URL |
| `approve(job_id)` | client | Pays 100% to the freelancer |
| `dispute(job_id)` | client or freelancer | Runs AI arbitration, state becomes `RESOLVED` |
| `settle(job_id)` | client or freelancer | Accepts the verdict and pays out |
| `appeal(job_id)` | client or freelancer | One re-evaluation, final and paid out |
| `get_job(job_id)` | anyone | Full job state including verdict and payout |
| `get_escrow_balance()` | anyone | GEN currently locked across unpaid jobs |
| `total_jobs()` | anyone | Number of jobs created |

## Running the tests

The tests use GenLayer Direct Mode, so the contract runs in memory. No Studio or Docker is required. Web and LLM calls are mocked deterministically.

    python3 -m venv .venv
    source .venv/bin/activate
    pip install genlayer-test==0.29.2
    gltest tests/ -v

Expected result: **14 passed**.

## What the tests cover

- Funding: escrow is locked on `create_job`, and `get_escrow_balance` tracks it.
- Happy path: client approval pays 100% and cannot be repeated.
- Dispute: partial payout computed from criterion weights (70% case); escrow stays locked until `settle` or `appeal`.
- Settle: accepting a verdict pays out and closes the job, and a settled job cannot be appealed.
- Clamping: out-of-range scores are clamped to 100.
- Appeal: exactly one appeal, it can change the outcome (50% to 90%), and a second appeal reverts.
- Access control and validation: only the freelancer submits, only the client approves, dispute requires submitted work, unfunded jobs revert, criteria weights must sum to 100, unknown job ids revert.

## Design notes and limitations

- Payouts go to externally owned accounts via `emit_transfer`.
- Only the first 8000 characters of the deliverable page are given to the LLM.
- There are no timeouts or auto-release if a client goes silent after submission. Either party can open a dispute instead.
- The appeal re-runs the same evaluation; it does not use a larger validator set.
- Tests mock web and LLM responses. Real-network behaviour depends on the deliverable page and the validators' LLMs.

## Repository layout

    contracts/freelance_escrow.py   the Intelligent Contract
    tests/direct/test_lifecycle.py  Direct Mode lifecycle tests
    pyproject.toml                  pinned test dependency (genlayer-test==0.29.2)
