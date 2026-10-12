# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import typing
from dataclasses import dataclass

from genlayer import *

# Lifecycle states
CREATED = "CREATED"      # job created and funded, freelancer assigned
SUBMITTED = "SUBMITTED"  # freelancer submitted a deliverable URL
RELEASED = "RELEASED"    # client approved, 100% paid to the freelancer
RESOLVED = "RESOLVED"    # AI arbitration issued a verdict (appeal or settle next)
FINAL = "FINAL"          # verdict is final (accepted or after the one appeal), paid out

PAYOUT_STEP = 10       # payout percentage is rounded to this step
PAYOUT_TOLERANCE = 10  # validators accept leader payouts within this many points


# Used to send GEN to an externally owned account. The empty View/Write bodies
# are intentional: only emit_transfer() is ever called on this interface.
@gl.evm.contract_interface
class _EOA:
    class View:
        pass

    class Write:
        pass


def _payout_pct(criteria: list, scores: list) -> int:
    """
    Deterministic payout percentage: sum(weight * score) over all criteria,
    each score clamped to [0, 1], rounded to PAYOUT_STEP. Criteria missing
    from the scores count as 0.
    """
    by_name = {}
    for s in scores:
        if isinstance(s, dict) and "name" in s:
            try:
                by_name[str(s["name"])] = float(s.get("score", 0))
            except (TypeError, ValueError):
                by_name[str(s["name"])] = 0.0
    total = 0.0
    for c in criteria:
        score = max(0.0, min(1.0, by_name.get(str(c["name"]), 0.0)))
        total += int(c["weight"]) * score
    pct = int(round(total / PAYOUT_STEP) * PAYOUT_STEP)
    return max(0, min(100, pct))


@allow_storage
@dataclass
class Job:
    client: Address
    freelancer: Address
    title: str
    criteria_json: str   # [{"name": "...", "weight": 50, "description": "..."}]
    amount: u256         # GEN locked in escrow for this job
    status: str
    deliverable_url: str
    verdict_json: str    # latest arbitration verdict
    freelancer_pct: u32  # share of the escrow owed to the freelancer
    appealed: bool
    paid: bool


class FreelanceEscrow(gl.Contract):
    """
    Freelance escrow with AI arbitration.

    The client locks GEN when creating a job together with weighted acceptance
    criteria. The freelancer submits a deliverable URL. The client can approve
    (100% to the freelancer) or either party can open a dispute: validators read
    the deliverable, score every criterion, and the contract computes the payout
    split deterministically from the criteria weights. The verdict can be
    appealed exactly once (re-evaluated by validators) or accepted via settle().
    The escrow is split on-chain between freelancer and client accordingly.
    """

    jobs: TreeMap[u32, Job]
    next_id: u32
    escrow_balance: u256  # GEN currently locked across all unpaid jobs

    def __init__(self):
        self.next_id = u32(0)
        self.escrow_balance = u256(0)

    # ---------- helpers ----------

    def _get(self, job_id: u32) -> Job:
        if job_id not in self.jobs:
            raise gl.vm.UserError("Unknown job id")
        return self.jobs[job_id]

    @staticmethod
    def _parse_criteria(criteria_json: str) -> list:
        try:
            criteria = json.loads(criteria_json)
        except ValueError:
            raise gl.vm.UserError("criteria_json must be valid JSON")
        if not isinstance(criteria, list) or len(criteria) == 0:
            raise gl.vm.UserError("criteria must be a non-empty list")
        for c in criteria:
            if not isinstance(c, dict) or "name" not in c or "weight" not in c:
                raise gl.vm.UserError("each criterion needs a name and a weight")
        if sum(int(c["weight"]) for c in criteria) != 100:
            raise gl.vm.UserError("criteria weights must sum to 100")
        return criteria

    def _evaluate(self, job: Job) -> typing.Any:
        criteria_json = job.criteria_json
        criteria = json.loads(criteria_json)
        url = job.deliverable_url
        title = job.title

        prompt = f"""You are a neutral arbiter of a freelance job.

JOB: {title}
ACCEPTANCE CRITERIA (JSON): {criteria_json}

Read the delivered work below. For EACH criterion, give a score from 0 to 1
(0 = not delivered, 1 = fully delivered) based ONLY on evidence in the
delivered work. Do not invent evidence and do not reward claims without proof.

Respond using ONLY the following JSON format, nothing else:
{{
  "scores": [{{"name": str, "score": float}}],  // one entry per criterion, using the exact criterion names
  "reasoning": str                              // two or three sentences citing the evidence
}}
This result must be perfectly parsable by a JSON parser without errors.
"""

        def leader_fn():
            response = gl.nondet.web.get(url)
            page = response.body.decode("utf-8")

            # Deterministic guard, identical on every validator: an empty or
            # unreachable deliverable is never sent to the LLM.
            if not page.strip():
                return {
                    "scores": [],
                    "payout_pct": 0,
                    "reasoning": "Deliverable was empty or unreachable; nothing to evaluate.",
                }

            raw = gl.nondet.exec_prompt(
                prompt + f"\n\nDELIVERED WORK:\n{page[:8000]}",
                response_format="json",
            )
            scores = raw.get("scores", [])
            return {
                "scores": scores,
                "payout_pct": _payout_pct(criteria, scores),
                "reasoning": str(raw.get("reasoning", "")),
            }

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            leader_out = leaders_res.calldata
            if "payout_pct" not in leader_out or "reasoning" not in leader_out:
                return False
            # The leader's payout must follow from its own scores and the weights.
            if int(leader_out["payout_pct"]) != _payout_pct(
                criteria, leader_out.get("scores", [])
            ):
                return False
            mine = leader_fn()
            return abs(int(mine["payout_pct"]) - int(leader_out["payout_pct"])) <= PAYOUT_TOLERANCE

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    def _apply_verdict(self, job: Job, verdict: typing.Any, new_status: str) -> None:
        job.verdict_json = json.dumps(verdict)
        job.freelancer_pct = u32(int(verdict["payout_pct"]))
        job.status = new_status

    def _pay_out(self, job: Job) -> None:
        """Split the escrow: freelancer gets freelancer_pct, the client gets the rest."""
        if job.paid:
