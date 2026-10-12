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
  "scores": [{{"name": str, "score": float}}],
  "reasoning": str
}}
Use the exact criterion names in "scores", one entry per criterion, and give
two or three sentences citing the evidence in "reasoning".
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
            expected = _payout_pct(criteria, leader_out.get("scores", []))
            if int(leader_out["payout_pct"]) != expected:
                return False
            mine = leader_fn()
            diff = abs(int(mine["payout_pct"]) - int(leader_out["payout_pct"]))
            return diff <= PAYOUT_TOLERANCE

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    def _apply_verdict(self, job: Job, verdict: typing.Any, new_status: str) -> None:
        job.verdict_json = json.dumps(verdict)
        job.freelancer_pct = u32(int(verdict["payout_pct"]))
        job.status = new_status

    def _pay_out(self, job: Job) -> None:
        """Split the escrow: freelancer gets freelancer_pct, the client gets the rest."""
        if job.paid:
            raise gl.vm.UserError("Already paid out")
        job.paid = True
        to_freelancer = u256(int(job.amount) * int(job.freelancer_pct) // 100)
        to_client = u256(int(job.amount) - int(to_freelancer))
        self.escrow_balance = u256(int(self.escrow_balance) - int(job.amount))
        if to_freelancer > u256(0):
            _EOA(job.freelancer).emit_transfer(value=to_freelancer)
        if to_client > u256(0):
            _EOA(job.client).emit_transfer(value=to_client)

    # ---------- lifecycle ----------

    @gl.public.write.payable
    def create_job(self, freelancer: Address, title: str, criteria_json: str) -> u32:
        """The attached GEN is locked as the escrow for this job."""
        amount = gl.message.value
        if amount == u256(0):
            raise gl.vm.UserError("Send some GEN to fund the escrow")
        if freelancer == gl.message.sender_address:
            raise gl.vm.UserError("Client and freelancer must be different accounts")
        self._parse_criteria(criteria_json)

        job_id = self.next_id
        self.jobs[job_id] = Job(
            client=gl.message.sender_address,
            freelancer=freelancer,
            title=title,
            criteria_json=criteria_json,
            amount=amount,
            status=CREATED,
            deliverable_url="",
            verdict_json="",
            freelancer_pct=u32(0),
            appealed=False,
            paid=False,
        )
        self.escrow_balance = u256(int(self.escrow_balance) + int(amount))
        self.next_id = u32(int(job_id) + 1)
        return job_id

    @gl.public.write
    def submit_work(self, job_id: u32, url: str) -> None:
        job = self._get(job_id)
        if gl.message.sender_address != job.freelancer:
            raise gl.vm.UserError("Only the freelancer can submit work")
        if job.status != CREATED:
            raise gl.vm.UserError("Job is not in CREATED state")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise gl.vm.UserError("Deliverable must be an http(s) URL")
        job.deliverable_url = url
        job.status = SUBMITTED
        self.jobs[job_id] = job

    @gl.public.write
    def approve(self, job_id: u32) -> None:
        job = self._get(job_id)
        if gl.message.sender_address != job.client:
            raise gl.vm.UserError("Only the client can approve")
        if job.status != SUBMITTED:
            raise gl.vm.UserError("Nothing to approve")
        job.freelancer_pct = u32(100)
        job.status = RELEASED
        self._pay_out(job)
        self.jobs[job_id] = job

    @gl.public.write
    def dispute(self, job_id: u32) -> typing.Any:
        job = self._get(job_id)
        if gl.message.sender_address not in (job.client, job.freelancer):
            raise gl.vm.UserError("Only the parties can dispute")
        if job.status != SUBMITTED:
            raise gl.vm.UserError("Job is not in SUBMITTED state")
        verdict = self._evaluate(job)
        self._apply_verdict(job, verdict, RESOLVED)
        self.jobs[job_id] = job
        return {
            "job_id": job_id,
            "status": job.status,
            "payout_pct": job.freelancer_pct,
        }

    @gl.public.write
    def appeal(self, job_id: u32) -> typing.Any:
        """One appeal per job: validators re-evaluate; the result is final and paid out."""
        job = self._get(job_id)
        if gl.message.sender_address not in (job.client, job.freelancer):
            raise gl.vm.UserError("Only the parties can appeal")
        if job.status != RESOLVED or job.appealed:
            raise gl.vm.UserError("Appeal not available")
        job.appealed = True
        verdict = self._evaluate(job)
        self._apply_verdict(job, verdict, FINAL)
        self._pay_out(job)
        self.jobs[job_id] = job
        return {
            "job_id": job_id,
            "status": job.status,
            "payout_pct": job.freelancer_pct,
        }

    @gl.public.write
    def settle(self, job_id: u32) -> None:
        """Accept the arbitration verdict without appealing: pay out and close the job."""
        job = self._get(job_id)
        if gl.message.sender_address not in (job.client, job.freelancer):
            raise gl.vm.UserError("Only the parties can settle")
        if job.status != RESOLVED:
            raise gl.vm.UserError("Nothing to settle")
        job.status = FINAL
        self._pay_out(job)
        self.jobs[job_id] = job

    # ---------- reads ----------

    @gl.public.view
    def get_job(self, job_id: u32) -> typing.Any:
        job = self._get(job_id)
        return {
            "client": job.client,
            "freelancer": job.freelancer,
            "title": job.title,
            "amount": int(job.amount),
            "status": job.status,
            "deliverable_url": job.deliverable_url,
            "verdict": json.loads(job.verdict_json) if job.verdict_json else None,
            "freelancer_pct": int(job.freelancer_pct),
            "freelancer_amount": int(job.amount) * int(job.freelancer_pct) // 100,
            "appealed": job.appealed,
            "paid": job.paid,
        }

    @gl.public.view
    def get_escrow_balance(self) -> u256:
        return self.escrow_balance

    @gl.public.view
    def total_jobs(self) -> u32:
        return self.next_id
