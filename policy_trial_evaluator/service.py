from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, Mapping
from policy_trial_evaluator.studies.trial_store import DomainError, RunState, SQLiteLedger, fingerprint, number
from policy_trial_evaluator.eligibility.rulebook import EligibilityRulebook
from policy_trial_evaluator.applications.intake import ApplicationIntake
from policy_trial_evaluator.allocation.quota import QuotaAllocation
from policy_trial_evaluator.baselines.matched_peers import MatchedPeers
from policy_trial_evaluator.outcomes.effect_size import EffectSize
from policy_trial_evaluator.audit.fairness import FairnessAudit

PROCESSORS = [EligibilityRulebook, ApplicationIntake, QuotaAllocation, MatchedPeers, EffectSize, FairnessAudit]

class ApplicationService:
    def __init__(self, database: str | Path):
        self.ledger = SQLiteLedger(database)
        self.processors = [factory() for factory in PROCESSORS]

    def normalize(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise DomainError("record must be an object")
        result = dict(value)
        case_id = str(result.get("case_id", "")).strip()
        if not case_id or len(case_id) > 96:
            raise DomainError("case_id is required and must be <= 96 characters")
        result["case_id"] = case_id
        result.setdefault("merchant_id", "merchant-synthetic")
        result.setdefault("market", "combined")
        result.setdefault("region", "haining")
        result.setdefault("status", "new")
        result.setdefault("source_system", "local-fixture")
        result.setdefault("event_date", "2026-11-12")
        result.setdefault("amount", 100.0)
        result.setdefault("count", 4)
        result.setdefault("total", 10)
        result.setdefault("metrics", {"conversion": 0.42, "traffic": 125, "repeat_rate": 0.31, "delay_hours": 2})
        result.setdefault("attributes", {"brand_level": "oem", "risk_score": 72, "channel": "offline", "originality": 0.8, "capacity": 20, "cost": 12.5})
        result.setdefault("tags", ["synthetic", "offline", "market"])
        result.setdefault("records", [{"id": "r1", "value": 80, "status": "open", "group": "leather"}, {"id": "r2", "value": 92, "status": "closed", "group": "textile"}])
        result.setdefault("timeline", [{"at": "2026-11-10", "start": 1, "end": 3}, {"at": "2026-11-11", "start": 4, "end": 8}])
        result.setdefault("links", [{"from": "leather", "to": "textile", "weight": 3}])
        return result

    def execute(self, value: Mapping[str, Any], request_key: str | None = None) -> dict[str, Any]:
        payload = self.normalize(value)
        key = request_key or fingerprint(payload)
        recalled = self.ledger.recalled(key)
        if recalled is not None:
            recalled["replayed"] = True
            return recalled
        state = RunState(uuid.uuid4().hex, payload["case_id"], payload)
        state.emit("received", "run_started", project='policy_trial_evaluator', architecture='资格规则、配额分配与准实验评估')
        try:
            self._run_trial_evaluation(state)
        except Exception as exc:
            self.ledger.fail(state.case_id, state.state, str(exc), payload)
            raise
        response = {
            "project": 'policy_trial_evaluator', "architecture": '资格规则、配额分配与准实验评估', "run_id": state.run_id,
            "case_id": state.case_id, "state": state.state, "revision": state.revision,
            "finding_count": len(state.findings), "findings": [item.rule for item in state.findings],
            "metrics": state.metrics, "artifacts": state.artifacts, "event_count": len(state.events),
            "storage": self.ledger.stats(), "replayed": False,
        }
        self.ledger.remember(key, state.run_id, response)
        return response

    def _run_trial_evaluation(self, state: RunState) -> None:
            state.artifacts['trial_protocol'] = {'eligible': None, 'allocation': None, 'effect': None}
            self.processors[0].process(state)
            self.processors[1].process(state)
            state.artifacts['trial_protocol']['eligible'] = not any(item.stage == 'eligibility_rulebook' and item.severity == 'critical' for item in state.findings)
            self.ledger.save(state)
            self.processors[2].process(state)
            state.artifacts['trial_protocol']['allocation'] = state.artifacts.get('quota_allocation', {}).get('domain_values')
            self.processors[3].process(state)
            self.processors[4].process(state)
            state.artifacts['trial_protocol']['effect'] = state.artifacts.get('effect_size', {}).get('domain_values')
            self.processors[5].process(state)
            state.emit('trial_projection', 'evaluation_closed', protocol=state.artifacts['trial_protocol'])
            state.advance('completed')
            self.ledger.save(state)

    def execute_many(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        accepted = []
        failures = []
        for index, record in enumerate(records):
            try:
                accepted.append(self.execute(record, request_key=f"batch:{index}:{fingerprint(record)}"))
            except Exception as exc:
                failures.append({"index": index, "error": str(exc)})
        return {"accepted": accepted, "failures": failures, "accepted_count": len(accepted), "failure_count": len(failures)}

def run_fixture(fixture: str | Path, database: str | Path) -> dict[str, Any]:
    data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    service = ApplicationService(database)
    return service.execute_many(data) if isinstance(data, list) else service.execute(data)
