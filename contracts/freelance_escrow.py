# { "Depends": "py-genlayer:test" }
# IMPORTANT: replace this header with the current one from your GenLayer Studio / docs,
# since the version hash depends on the SDK release.

import json
from dataclasses import dataclass

from genlayer import *

# Lifecycle states
CREATED = "CREATED"        # job created, freelancer assigned
SUBMITTED = "SUBMITTED"    # freelancer submitted a deliverable (URL)
RELEASED = "RELEASED"      # client approved, 100% to the freelancer
RESOLVED = "RESOLVED"      # AI arbitration issued a verdict (one appeal allowed)
FINAL = "FINAL"            # verdict after appeal, final

PAYOUT_STEP = 10  # payout percentage is rounded to this step so validators can agree


@allow_storage
@dataclass
class Job:
    client: Address
    freelancer: Address
    title: str
    criteria_json: str       # [{"name": "...", "weight": 50, "description": "..."}]
    amount: u256             # accounting-only escrow amount (no native transfers yet)
    status: str
    deliverable_url: str
    verdict_json: str        # latest arbitration verdict
    freelancer_pct: u256     # final payout percentage to the freelancer
    appealed: bool


class FreelanceEscrow(gl.Contract):
    jobs: TreeMap[u256, Job]
    next_id: u256

    def __init__(self):
        self.next_id = u256(1)

    # ---------- helpers ----------

    def _get(self, job_id: int) -> Job:
        jid = u256(job_id)
        if jid not in self.jobs:
            raise gl.vm.UserError("job not found")
        return self.jobs[jid]

    @staticmethod
    def _validate_criteria(criteria_json: str) -> None:
        criteria = json.loads(criteria_json)
        if not isinstance(criteria, list) or len(criteria) == 0:
            raise gl.vm.UserError("criteria must be a non-empty list")
        if sum(int(c["weight"]) for c in criteria) != 100:
            raise gl.vm.UserError("criteria weights must sum to 100")

    def _evaluate(self, job: Job) -> dict:
        criteria = job.criteria_json
        url = job.deliverable_url
        title = job.title

        def leader_fn():
            page = gl.nondet.web.render(url, mode="text")
            prompt = f"""
You are a neutral arbiter of a freelance job.
Job: {title}
Acceptance criteria (JSON, weights sum to 100): {criteria}
Delivered work (page text, may be truncated):
{page[:6000]}

Score each criterion from 0 to 1 based ONLY on evidence in the delivered work.
Compute payout_pct = sum(weight * score), round to the nearest {PAYOUT_STEP}.
Return JSON only:
{{"scores": [{{"name": "...", "score": 0.0}}], "payout_pct": 0, "reasoning": "short"}}
"""
            result = gl.nondet.exec_prompt(prompt, response_format="json")
            pct = int(round(float(result["payout_pct"]) / PAYOUT_STEP) * PAYOUT_STEP)
            result["payout_pct"] = max(0, min(100, pct))
            return result

        return gl.eq_principle.prompt_comparative(
            leader_fn,
            "The payout_pct values must be equal or differ by at most 10 points.",
        )

    def _apply_verdict(self, job: Job, verdict: dict, final_status: str) -> None:
        job.verdict_json = json.dumps(verdict)
        job.freelancer_pct = u256(int(verdict["payout_pct"]))
        job.status = final_status

    # ---------- lifecycle ----------

    @gl.public.write
    def create_job(self, freelancer: str, title: str, criteria_json: str, amount: int) -> int:
        self._validate_criteria(criteria_json)
        if amount <= 0:
            raise gl.vm.UserError("amount must be positive")
        jid = self.next_id
        self.jobs[jid] = Job(
            client=gl.message.sender_address,
            freelancer=Address(freelancer),
            title=title,
            criteria_json=criteria_json,
            amount=u256(amount),
            status=CREATED,
            deliverable_url="",
            verdict_json="",
            freelancer_pct=u256(0),
            appealed=False,
        )
        self.next_id = u256(int(jid) + 1)
        return int(jid)

    @gl.public.write
    def submit_work(self, job_id: int, url: str) -> None:
        job = self._get(job_id)
        if gl.message.sender_address != job.freelancer:
            raise gl.vm.UserError("only freelancer can submit")
        if job.status != CREATED:
            raise gl.vm.UserError("job not in CREATED state")
        job.deliverable_url = url
        job.status = SUBMITTED

    @gl.public.write
    def approve(self, job_id: int) -> None:
        job = self._get(job_id)
        if gl.message.sender_address != job.client:
            raise gl.vm.UserError("only client can approve")
        if job.status != SUBMITTED:
            raise gl.vm.UserError("nothing to approve")
        job.freelancer_pct = u256(100)
        job.status = RELEASED

    @gl.public.write
    def dispute(self, job_id: int) -> None:
        job = self._get(job_id)
        if gl.message.sender_address not in (job.client, job.freelancer):
            raise gl.vm.UserError("only parties can dispute")
        if job.status != SUBMITTED:
            raise gl.vm.UserError("job not in SUBMITTED state")
        self._apply_verdict(job, self._evaluate(job), RESOLVED)

    @gl.public.write
    def appeal(self, job_id: int) -> None:
        job = self._get(job_id)
        if gl.message.sender_address not in (job.client, job.freelancer):
            raise gl.vm.UserError("only parties can appeal")
        if job.status != RESOLVED or job.appealed:
            raise gl.vm.UserError("appeal not available")
        job.appealed = True
        self._apply_verdict(job, self._evaluate(job), FINAL)

    # ---------- views ----------

    @gl.public.view
    def get_job(self, job_id: int) -> dict:
        job = self._get(job_id)
        return {
            "client": job.client.as_hex,
            "freelancer": job.freelancer.as_hex,
            "title": job.title,
            "amount": int(job.amount),
            "status": job.status,
            "deliverable_url": job.deliverable_url,
            "verdict": json.loads(job.verdict_json) if job.verdict_json else None,
            "freelancer_pct": int(job.freelancer_pct),
            "freelancer_amount": int(job.amount) * int(job.freelancer_pct) // 100,
            "appealed": job.appealed,
        }
