"""分批发放（rollout）政策效果研究引擎。

把“获批（approved_at）”与“到账（paid_at）”两条时间线接起来，按首次有效到账周
定义发放批次，并在“同一经营地区内仍有未受扶持商户”的批次上做可复查的准实验
估计。核心约定：

* 处理（treatment）定义为首次有效到账，不是资格通过，也不是获批；
* 对照必须同地区、在目标批次整个观察窗口内尚未获批且尚未到账；
* 对照筛选只使用干预前信息（地区、干预前销售水平），绝不使用 post_staff 或
  干预后销售等政策之后形成的特征；
* 没有同地区干净对照的批次明确记为 not_identified，不返回 0，也不跨地区硬凑；
* 分析截止 as_of 之后才得知的修订不参与识别；study_id 由截止时点可见的分析
  切片决定，旧研究不会被迟到修订改写；
* 不修改原有资格、申请、额度与审批数据，只新增可引用的研究记录。

设计参数集中在本模块顶部的常量中，并写入 source_versions 与研究记录。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from policy_trial_evaluator.studies.trial_store import SQLiteLedger, stable_json, utc_now

CODE_VERSION = "rollout-study/1"

PRE_WEEKS = 8
POST_WEEKS = 8
PLACEBO_SHIFT_WEEKS = 8
PLACEBO_PRE_WEEKS = 4
PLACEBO_POST_WEEKS = 4
BOOTSTRAP_REPLICATES = 1000
PERMUTATION_REPLICATES = 1000
SUPPORT_MARGIN = 0.25
ALPHA = 0.05
INFLUENCE_DROP_RATIO = 0.25
CONCENTRATION_LIMIT = 0.50
MAX_DROP = 3
MIN_GROUP_SIZE = 2
Z_975 = 1.959963984540054

PARAMETERS = {
    "pre_weeks": PRE_WEEKS,
    "post_weeks": POST_WEEKS,
    "placebo_shift_weeks": PLACEBO_SHIFT_WEEKS,
    "placebo_pre_weeks": PLACEBO_PRE_WEEKS,
    "placebo_post_weeks": PLACEBO_POST_WEEKS,
    "bootstrap_replicates": BOOTSTRAP_REPLICATES,
    "permutation_replicates": PERMUTATION_REPLICATES,
    "support_margin": SUPPORT_MARGIN,
    "alpha": ALPHA,
    "influence_drop_ratio_limit": INFLUENCE_DROP_RATIO,
    "concentration_limit": CONCENTRATION_LIMIT,
    "max_drop": MAX_DROP,
    "min_group_size": MIN_GROUP_SIZE,
}

PAID_STATUSES = {"paid", "settled", "disbursed", "completed"}
REVERSED_STATUSES = {"reversed", "cancelled", "canceled", "returned", "failed", "void"}
PENDING_STATUSES = {"approved", "pending", "scheduled", "awarded", "queued"}

PRIMARY_ESTIMATOR = "trend_adjusted_did"
STRENGTH_ORDER = {"supported": 0, "partial": 1, "average_only": 2, "weakened": 3, "unidentified": 4}


class StudyInputError(ValueError):
    """输入不满足研究协议时抛出。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _round(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(sorted_values: Sequence[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = quantile * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def _slope(values: Sequence[float]) -> float:
    """对等间隔周序列拟合线性斜率（单位：元/周）。"""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = _mean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(n))
    if denominator <= 0:
        return 0.0
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(matrix)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [augmented[row][size] for row in range(size)]


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    size = len(matrix)
    augmented = [
        list(matrix[row]) + [1.0 if row == column else 0.0 for column in range(size)]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [row[size:] for row in augmented]


def _fit(rows: Sequence[tuple[float, float, float, float]], adjust_trend: bool) -> dict[str, Any] | None:
    """回归调整 DiD：delta ~ treat + pre_mean_centered (+ pre_slope_centered)。

    rows 每项为 (treat, pre_mean, pre_slope, delta)。协变量先中心化以避免
    截距与约 600 元的销售水平共线带来的数值误差；treat 系数不受中心化影响。
    返回 treat 系数与 HC1 标准误。
    """
    if not rows:
        return None
    count = len(rows)
    treated_count = sum(1 for row in rows if row[0] >= 0.5)
    control_count = count - treated_count
    if treated_count < 1 or control_count < 1:
        return None
    pre_values = [row[1] for row in rows]
    slope_values = [row[2] for row in rows]
    pre_center = _mean(pre_values)
    slope_center = _mean(slope_values)
    design: list[list[float]] = []
    outcome: list[float] = []
    for treat, pre_mean, pre_slope, delta in rows:
        row = [1.0, float(treat), float(pre_mean) - pre_center]
        if adjust_trend:
            row.append(float(pre_slope) - slope_center)
        design.append(row)
        outcome.append(float(delta))
    width = len(design[0])
    if count <= width + 1:
        return None
    xtx = [
        [sum(design[i][a] * design[i][b] for i in range(count)) for b in range(width)]
        for a in range(width)
    ]
    xty = [sum(design[i][a] * outcome[i] for i in range(count)) for a in range(width)]
    beta = _solve(xtx, xty)
    inverse = _inverse(xtx)
    if beta is None or inverse is None:
        return None
    residuals = [
        outcome[i] - sum(design[i][j] * beta[j] for j in range(width)) for i in range(count)
    ]
    scale = count / max(count - width, 1)
    meat = [
        [sum(design[i][a] * design[i][b] * residuals[i] ** 2 for i in range(count)) for b in range(width)]
        for a in range(width)
    ]
    covariance = [
        [
            sum(
                inverse[a][u] * meat[u][v] * inverse[v][b]
                for u in range(width)
                for v in range(width)
            )
            for b in range(width)
        ]
        for a in range(width)
    ]
    variance = covariance[1][1] * scale
    return {
        "estimate": float(beta[1]),
        "se": math.sqrt(max(variance, 0.0)),
        "n": count,
        "columns": ["intercept", "treat", "pre_mean_centered"]
        + (["pre_slope_centered"] if adjust_trend else []),
    }


def _bootstrap_estimate(
    treated_rows: Sequence[tuple[float, float, float, float]],
    control_rows: Sequence[tuple[float, float, float, float]],
    adjust_trend: bool,
    replicates: int,
    seed: str,
) -> list[float]:
    if not treated_rows or not control_rows:
        return []
    rng = random.Random(_seed(seed))
    draws: list[float] = []
    for _ in range(replicates):
        treated = [treated_rows[rng.randrange(len(treated_rows))] for _ in treated_rows]
        controls = [control_rows[rng.randrange(len(control_rows))] for _ in control_rows]
        fitted = _fit(list(treated) + list(controls), adjust_trend)
        if fitted is not None:
            draws.append(fitted["estimate"])
    return draws


def _seed(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


# ---------------------------------------------------------------------------
# 输入、时点与事件解析
# ---------------------------------------------------------------------------

def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _visible(record: Mapping[str, Any], as_of: date) -> bool:
    known = _parse_date(record.get("known_at"))
    if known is None:
        return True
    return known <= as_of


def _status_bucket(status: Any) -> str:
    text = str(status or "").strip().casefold()
    if text in PAID_STATUSES:
        return "paid"
    if text in REVERSED_STATUSES:
        return "reversed"
    if text in PENDING_STATUSES:
        return "pending"
    return "unknown"


class _Prepared:
    """一次研究的可复查输入切片。"""

    def __init__(self, payload: Mapping[str, Any], as_of: date | None = None) -> None:
        self.payload = payload
        self.as_of = as_of or _parse_date(payload.get("as_of"))
        if self.as_of is None:
            raise StudyInputError("as_of is required for a reproducible study")
        self.weeks: list[str] = []
        self.sales: dict[tuple[str, str], float] = {}
        self.region: dict[str, str] = {}
        self.merchant_weeks: dict[str, set[str]] = defaultdict(set)
        self.panel_rows_total = 0
        self.panel_rows_visible = 0
        self.payment_rows_total = 0
        self.payment_rows_visible = 0
        self.payments_late_revisions: list[dict[str, Any]] = []
        self.payments: dict[str, dict[str, Any]] = {}
        self.first_approval: dict[str, date] = {}
        self.first_paid: dict[str, date] = {}
        self.paid_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.reversed_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.pending_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.effective_payment_refs: dict[str, dict[str, Any]] = {}
        self.duplicate_reports: list[str] = []
        self.exposure_changes: list[dict[str, Any]] = []
        self._read_panel()
        self._read_payments()
        self._resolve_merchants()
        self.analysis_fingerprint = self._analysis_fingerprint()
        self.study_key = self._study_key()

    # -- panel ------------------------------------------------------------
    def _read_panel(self) -> None:
        panel = self.payload.get("panel")
        if not isinstance(panel, list) or not panel:
            raise StudyInputError("panel must be a non-empty list")
        week_set: set[str] = set()
        for record in panel:
            self.panel_rows_total += 1
            if not isinstance(record, Mapping):
                raise StudyInputError("panel rows must be objects")
            merchant = str(record.get("merchant_id", "")).strip()
            week = str(record.get("week", "")).strip()
            if not merchant or not week:
                raise StudyInputError("panel rows require merchant_id and week")
            if not _visible(record, self.as_of):
                continue
            sales = record.get("sales")
            if isinstance(sales, bool) or not isinstance(sales, (int, float)):
                raise StudyInputError(f"panel sales must be numeric for {merchant} {week}")
            if not math.isfinite(float(sales)):
                raise StudyInputError(f"panel sales must be finite for {merchant} {week}")
            parsed_week = _parse_date(week)
            if parsed_week is None:
                raise StudyInputError(f"invalid week {week}")
            week_set.add(week)
            self.sales[(merchant, week)] = float(sales)
            self.merchant_weeks[merchant].add(week)
            region = record.get("pre_region")
            if region is not None:
                text = str(region).strip()
                if text and self.region.get(merchant, text) != text:
                    raise StudyInputError(f"pre_region changes within merchant {merchant}")
                self.region[merchant] = text
            elif merchant not in self.region:
                self.region[merchant] = "unknown"
            self.panel_rows_visible += 1
        self.weeks = sorted(week_set)
        if len(self.weeks) < PRE_WEEKS + POST_WEEKS:
            raise StudyInputError("panel needs enough weeks for pre and post windows")
        for merchant in self.merchant_weeks:
            self.region.setdefault(merchant, "unknown")

    # -- payments ---------------------------------------------------------
    def _read_payments(self) -> None:
        payments = self.payload.get("payments", [])
        if not isinstance(payments, list):
            raise StudyInputError("payments must be a list")
        visible_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        late_ids: set[str] = set()
        for record in payments:
            self.payment_rows_total += 1
            if not isinstance(record, Mapping):
                raise StudyInputError("payment rows must be objects")
            payment_id = str(record.get("payment_id", "")).strip()
            merchant = str(record.get("merchant_id", "")).strip()
            if not payment_id or not merchant:
                raise StudyInputError("payment rows require payment_id and merchant_id")
            if not _visible(record, self.as_of):
                late_ids.add(payment_id)
                continue
            visible_by_id[payment_id].append(dict(record))
            self.payment_rows_visible += 1
        # 每个 payment_id 只取截至 as_of 可见的最高修订版本；迟到修订单独登记。
        for payment_id, records in visible_by_id.items():
            ordered = sorted(
                records,
                key=lambda item: (int(item.get("revision") or 0), str(item.get("known_at") or "")),
            )
            for stale in ordered[:-1]:
                self.duplicate_reports.append(str(stale.get("payment_id")))
            self.payments[payment_id] = ordered[-1]
        for payment_id in sorted(late_ids - set(visible_by_id)):
            self.payments_late_revisions.append({"payment_id": payment_id, "reason": "only_revision_after_as_of"})
        for payment_id in sorted(late_ids & set(visible_by_id)):
            self.payments_late_revisions.append({"payment_id": payment_id, "reason": "newer_revision_after_as_of"})

    def _resolve_merchants(self) -> None:
        for payment_id, record in sorted(self.payments.items()):
            merchant = str(record["merchant_id"]).strip()
            bucket = _status_bucket(record.get("status"))
            approved = _parse_date(record.get("approved_at"))
            paid = _parse_date(record.get("paid_at"))
            event = {
                "payment_id": payment_id,
                "merchant_id": merchant,
                "status": str(record.get("status") or ""),
                "approved_at": approved.isoformat() if approved else None,
                "paid_at": paid.isoformat() if paid else None,
                "known_at": str(record.get("known_at") or ""),
                "revision": int(record.get("revision") or 0),
            }
            if approved is not None:
                current = self.first_approval.get(merchant)
                if current is None or approved < current:
                    self.first_approval[merchant] = approved
            if bucket == "paid" and paid is not None:
                self.paid_events[merchant].append(event)
                self.effective_payment_refs[payment_id] = {
                    "payment_id": payment_id,
                    "merchant_id": merchant,
                    "approved_at": event["approved_at"],
                    "paid_at": event["paid_at"],
                    "status": event["status"],
                    "revision": event["revision"],
                }
                current = self.first_paid.get(merchant)
                if current is None or paid < current:
                    self.first_paid[merchant] = paid
            elif bucket == "reversed":
                # 冲正/退出：在 as_of 可见修订中已不再有效。若曾到账则记为退出，
                # 不进处理组，也不当对照，避免把已暴露商户悄悄当成未干预。
                self.reversed_events[merchant].append(event)
            elif bucket == "pending":
                self.pending_events[merchant].append(event)
        for merchant, events in self.paid_events.items():
            events.sort(key=lambda item: (item["paid_at"], item["payment_id"]))
            later = [event for event in events if event["paid_at"] != events[0]["paid_at"]]
            if later:
                self.exposure_changes.append(
                    {
                        "merchant_id": merchant,
                        "kind": "reissued",
                        "first_paid_at": events[0]["paid_at"],
                        "later_payments": [event["payment_id"] for event in later],
                        "rule": "处理时点取首次有效到账；后续到账不改写批次，只登记。",
                    }
                )
        for merchant, events in self.reversed_events.items():
            self.exposure_changes.append(
                {
                    "merchant_id": merchant,
                    "kind": "exited_by_reversal",
                    "payments": [event["payment_id"] for event in events],
                    "paid_at": sorted(event["paid_at"] for event in events if event["paid_at"]),
                    "rule": "as_of 可见修订中状态为冲正/取消的到账不算有效处理；该商户不进处理组，也不作对照。",
                }
            )

    # -- fingerprints -----------------------------------------------------
    def _analysis_payload(self) -> dict[str, Any]:
        panel_slice = sorted(
            [merchant, week, self.sales[(merchant, week)], self.region.get(merchant, "unknown")]
            for merchant, week in self.sales
        )
        payment_slice = sorted(
            [
                [
                    payment_id,
                    record["merchant_id"],
                    record["approved_at"],
                    record["paid_at"],
                    record["status"],
                    record["revision"],
                    record["known_at"],
                ]
                for payment_id, record in self.payments.items()
            ]
        )
        return {
            "as_of": self.as_of.isoformat(),
            "parameters": PARAMETERS,
            "panel": panel_slice,
            "payments": payment_slice,
        }

    def _analysis_fingerprint(self) -> str:
        return hashlib.sha256(stable_json(self._analysis_payload()).encode("utf-8")).hexdigest()

    def _study_key(self) -> str:
        payload = {
            "code_version": CODE_VERSION,
            "analysis_fingerprint": self.analysis_fingerprint,
            "parameters": PARAMETERS,
        }
        return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()

    @property
    def merchants(self) -> list[str]:
        return sorted(self.merchant_weeks)

    def week_index(self, value: date | None) -> int | None:
        if value is None:
            return None
        index = bisect_right(self.weeks, value.isoformat()) - 1
        return index if index >= 0 else None

    def series(self, merchant: str, indices: Sequence[int]) -> list[float] | None:
        values = []
        for index in indices:
            key = (merchant, self.weeks[index])
            if key not in self.sales:
                return None
            values.append(self.sales[key])
        return values

    def category(self, merchant: str) -> str:
        if merchant in self.first_paid:
            return "treated"
        if merchant in self.reversed_events:
            return "exited_by_reversal"
        if merchant in self.first_approval or self.pending_events.get(merchant):
            return "approved_not_paid"
        return "control_candidate"


# ---------------------------------------------------------------------------
# 研究构造
# ---------------------------------------------------------------------------

def _unit(study: _Prepared, merchant: str, pre: Sequence[int], post: Sequence[int]) -> dict[str, Any] | None:
    pre_values = study.series(merchant, pre)
    post_values = study.series(merchant, post)
    if pre_values is None or post_values is None:
        return None
    pre_mean = _mean(pre_values)
    return {
        "merchant_id": merchant,
        "pre_mean": pre_mean,
        "pre_slope": _slope(pre_values),
        "delta": _mean(post_values) - pre_mean,
    }


def _row(unit: Mapping[str, Any], treated: bool) -> tuple[float, float, float, float]:
    return (1.0 if treated else 0.0, unit["pre_mean"], unit["pre_slope"], unit["delta"])


def _support_bounds(treated: Sequence[Mapping[str, Any]]) -> tuple[float, float, float]:
    lows = [unit["pre_mean"] for unit in treated]
    low, high = min(lows), max(lows)
    margin = SUPPORT_MARGIN * (high - low)
    if margin <= 0:
        margin = max(abs(high) * SUPPORT_MARGIN, 1.0)
    return low - margin, high + margin, margin


def _admissible(controls: Sequence[Mapping[str, Any]], bounds: tuple[float, float]) -> list[dict[str, Any]]:
    low, high = bounds
    return [unit for unit in controls if low <= unit["pre_mean"] <= high]


def _interval(draws: Sequence[float], point: float, se: float) -> tuple[list[float] | None, str]:
    if len(draws) >= max(50, BOOTSTRAP_REPLICATES // 4):
        ordered = sorted(draws)
        return [_round(_percentile(ordered, ALPHA / 2)), _round(_percentile(ordered, 1 - ALPHA / 2))], "merchant_cluster_bootstrap"
    if math.isfinite(se):
        return [_round(point - Z_975 * se), _round(point + Z_975 * se)], "hc1_normal_fallback"
    return None, "unavailable"


def _pretrend_check(
    treated: Sequence[Mapping[str, Any]],
    controls: Sequence[Mapping[str, Any]],
    seed: str,
) -> dict[str, Any]:
    treated_slope = _mean([unit["pre_slope"] for unit in treated])
    control_slope = _mean([unit["pre_slope"] for unit in controls])
    observed = treated_slope - control_slope
    pool = [unit["pre_slope"] for unit in treated] + [unit["pre_slope"] for unit in controls]
    if len(pool) < 4 or len(treated) < MIN_GROUP_SIZE or len(controls) < MIN_GROUP_SIZE:
        return {
            "status": "unavailable",
            "treated_pre_slope": _round(treated_slope),
            "control_pre_slope": _round(control_slope),
            "slope_difference": _round(observed),
            "permutation_p_value": None,
            "pass": None,
        }
    rng = random.Random(_seed(seed))
    treated_count = len(treated)
    extreme = 0
    for _ in range(PERMUTATION_REPLICATES):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        difference = _mean(shuffled[:treated_count]) - _mean(shuffled[treated_count:])
        if abs(difference) >= abs(observed) - 1e-12:
            extreme += 1
    p_value = (extreme + 1) / (PERMUTATION_REPLICATES + 1)
    return {
        "status": "tested",
        "treated_pre_slope": _round(treated_slope),
        "control_pre_slope": _round(control_slope),
        "slope_difference": _round(observed),
        "permutation_p_value": _round(p_value),
        "pass": p_value >= ALPHA,
        "test": "label permutation on pre-period slopes",
    }


def _placebo_check(
    study: _Prepared,
    cohort: Mapping[str, Any],
    seed: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    if cohort["status"] != "estimated":
        return {"status": "skipped", "reason": "cohort_not_identified", "pass": None}
    payout_index = cohort["payout_index"]
    fake_payout = payout_index - PLACEBO_SHIFT_WEEKS
    pre = list(range(fake_payout - PLACEBO_PRE_WEEKS, fake_payout))
    post = list(range(fake_payout, fake_payout + PLACEBO_POST_WEEKS))
    if not pre or pre[0] < 0 or post[-1] >= len(study.weeks):
        return {"status": "unavailable", "reason": "insufficient_placebo_window", "pass": None}
    post_end = post[-1]
    controls: list[dict[str, Any]] = []
    excluded_exposed = 0
    for merchant in study.merchants:
        if merchant in set(cohort["treated_ids"]):
            continue
        if study.region.get(merchant) != cohort["region"]:
            continue
        approval = study.week_index(study.first_approval.get(merchant))
        paid = study.week_index(study.first_paid.get(merchant))
        if merchant in study.reversed_events:
            excluded_exposed += 1
            continue
        if (approval is not None and approval <= post_end) or (paid is not None and paid <= post_end):
            excluded_exposed += 1
            continue
        unit = _unit(study, merchant, pre, post)
        if unit is None:
            continue
        controls.append(unit)
    treated_units = [unit for unit in (_unit(study, m, pre, post) for m in cohort["treated_ids"]) if unit]
    if len(controls) < MIN_GROUP_SIZE or len(treated_units) < MIN_GROUP_SIZE:
        return {
            "status": "unavailable",
            "reason": "no_clean_controls_for_placebo_window",
            "clean_controls": len(controls),
            "excluded_already_exposed": excluded_exposed,
            "pass": None,
        }
    bounds = _support_bounds(treated_units)
    admissible = _admissible(controls, bounds[:2])
    if len(admissible) < MIN_GROUP_SIZE:
        return {
            "status": "unavailable",
            "reason": "no_common_support_for_placebo_window",
            "clean_controls": len(controls),
            "admissible_controls": len(admissible),
            "excluded_already_exposed": excluded_exposed,
            "pass": None,
        }
    treated_rows = [_row(unit, True) for unit in treated_units]
    control_rows = [_row(unit, False) for unit in admissible]
    fitted = _fit(treated_rows + control_rows, adjust_trend=False)
    if fitted is None:
        return {"status": "unavailable", "reason": "singular_design", "pass": None}
    draws = _bootstrap_estimate(
        treated_rows, control_rows, False, BOOTSTRAP_REPLICATES, seed + "|placebo"
    )
    interval, method = _interval(draws, fitted["estimate"], fitted["se"])
    covers_zero = bool(interval and interval[0] <= 0 <= interval[1])
    diagnostics.setdefault("placebo_runs", {})[cohort["cohort"]] = {
        "fake_payout_week": study.weeks[fake_payout],
        "pre_weeks": [study.weeks[i] for i in pre],
        "post_weeks": [study.weeks[i] for i in post],
        "clean_controls": len(admissible),
        "excluded_already_exposed": excluded_exposed,
        "estimator": "level_adjusted_did",
    }
    return {
        "status": "tested",
        "fake_payout_week": study.weeks[fake_payout],
        "estimate": _round(fitted["estimate"]),
        "interval": interval,
        "interval_method": method,
        "covers_zero": covers_zero,
        "pass": covers_zero and len(draws) > 0,
        "clean_controls": len(admissible),
        "excluded_already_exposed": excluded_exposed,
    }


def _influence_check(
    study: _Prepared,
    cohort: Mapping[str, Any],
    treated_units: Sequence[Mapping[str, Any]],
    control_units: Sequence[Mapping[str, Any]],
    point: float,
    seed: str,
) -> dict[str, Any]:
    ordered = sorted(treated_units, key=lambda unit: unit["pre_mean"], reverse=True)
    drops = []
    max_change = 0.0
    sign_flip = False
    for count in range(1, min(MAX_DROP, len(ordered) - 1) + 1):
        kept = ordered[count:]
        fitted = _fit([_row(unit, True) for unit in kept] + [_row(unit, False) for unit in control_units], True)
        if fitted is None:
            continue
        change = abs(fitted["estimate"] - point)
        relative = change / abs(point) if abs(point) > 1e-9 else None
        if relative is not None:
            max_change = max(max_change, relative)
        if point != 0 and fitted["estimate"] * point < 0:
            sign_flip = True
        drops.append(
            {
                "drop_count": count,
                "dropped_pre_sales": [_round(unit["pre_mean"]) for unit in ordered[:count]],
                "estimate": _round(fitted["estimate"]),
                "relative_change": _round(relative),
            }
        )
    control_delta = _mean([unit["delta"] for unit in control_units])
    contributions = sorted(
        (unit["delta"] - control_delta for unit in treated_units), reverse=True
    )
    top_count = max(1, math.ceil(0.1 * len(contributions)))
    total = sum(contributions)
    # 集中度只在总效应为正时才有“少数大商户贡献了多数效应”的含义。
    top_share = sum(contributions[:top_count]) / total if total > 1e-9 else None
    median_effect = statistics.median(contributions) if contributions else None
    positive_share = sum(1 for value in contributions if value > 0) / len(contributions) if contributions else None
    return {
        "drop_largest": drops,
        "max_relative_change": _round(max_change),
        "sign_flip": sign_flip,
        "influence_pass": bool(drops) and max_change <= INFLUENCE_DROP_RATIO and not sign_flip,
        "top_decile_share": _round(top_share),
        "median_merchant_effect": _round(median_effect),
        "positive_merchant_share": _round(positive_share),
        "concentration_pass": top_share is not None and top_share <= CONCENTRATION_LIMIT,
        "top_decile_count": top_count,
    }


def _cohort_result(
    study: _Prepared,
    cohort_id: str,
    region: str,
    approved_date: date,
    payout_date: date,
    members: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    approval_index = study.week_index(approved_date)
    payout_index = study.week_index(payout_date)
    cohort_record: dict[str, Any] = {
        "cohort": cohort_id,
        "approved_at": approved_date.isoformat(),
        "paid_at": payout_date.isoformat(),
        "payout_week": study.weeks[payout_index] if payout_index is not None else None,
        "payout_index": payout_index,
        "region": region,
        "treatment_definition": "first effective payout (paid_at), not approval or eligibility",
        "treated_ids": sorted(members),
        "treated_count": len(members),
        "status": "not_identified",
        "reason": None,
        "control_ids": [],
        "control_pool_size": 0,
        "admissible_control_count": 0,
        "support": None,
    }
    if approval_index is None or payout_index is None:
        cohort_record["reason"] = "missing_approval_or_payout_date"
        return cohort_record, None
    pre = list(range(approval_index - PRE_WEEKS, approval_index))
    post = list(range(payout_index, payout_index + POST_WEEKS))
    if pre[0] < 0:
        cohort_record["reason"] = "insufficient_pre_period"
        return cohort_record, None
    if post[-1] >= len(study.weeks):
        cohort_record["reason"] = "post_window_beyond_as_of"
        return cohort_record, None
    post_end = post[-1]

    treated_units: list[dict[str, Any]] = []
    missing_treated: list[str] = []
    for merchant in sorted(members):
        unit = _unit(study, merchant, pre, post)
        if unit is None:
            missing_treated.append(merchant)
        else:
            treated_units.append(unit)
    cohort_record["missing_treated_windows"] = missing_treated
    if len(treated_units) < MIN_GROUP_SIZE:
        cohort_record["reason"] = "insufficient_treated_units"
        return cohort_record, None

    control_units: list[dict[str, Any]] = []
    excluded_exposed: list[str] = []
    excluded_incomplete: list[str] = []
    never_treated = 0
    not_yet_treated = 0
    treated_set = set(members)
    for merchant in study.merchants:
        if merchant in treated_set:
            continue
        if study.region.get(merchant) != region:
            continue
        approval = study.week_index(study.first_approval.get(merchant))
        paid = study.week_index(study.first_paid.get(merchant))
        if merchant in study.reversed_events:
            excluded_exposed.append(merchant)
            continue
        if (approval is not None and approval <= post_end) or (paid is not None and paid <= post_end):
            excluded_exposed.append(merchant)
            continue
        unit = _unit(study, merchant, pre, post)
        if unit is None:
            excluded_incomplete.append(merchant)
            continue
        control_units.append(unit)
        if merchant not in study.first_approval and merchant not in study.first_paid:
            never_treated += 1
        else:
            not_yet_treated += 1
    cohort_record["control_ids"] = sorted(unit["merchant_id"] for unit in control_units)
    cohort_record["control_pool_size"] = len(control_units)
    cohort_record["control_composition"] = {
        "never_approved_or_paid": never_treated,
        "approved_or_paid_after_window_only": not_yet_treated,
        "note": "not-yet-treated 只在其获批且到账都晚于本批次观察窗口时进入对照；窗口内暴露者全部排除。",
    }
    cohort_record["excluded_already_exposed"] = sorted(excluded_exposed)
    cohort_record["excluded_incomplete_windows"] = sorted(excluded_incomplete)
    cohort_record["windows"] = {
        "pre_weeks": [study.weeks[index] for index in pre],
        "post_weeks": [study.weeks[index] for index in post],
        "anticipation_gap_weeks": [study.weeks[index] for index in range(approval_index, payout_index)],
        "anticipation_note": "获批到到账之间留作缓冲周，不进入 pre 或 post 均值，避免把获批后的预期反应算进干预前趋势。",
    }

    if len(control_units) < MIN_GROUP_SIZE:
        cohort_record["reason"] = "no_clean_same_region_controls_through_post_window"
        cohort_record["support"] = {
            "treated_pre_range": [
                _round(min(unit["pre_mean"] for unit in treated_units)),
                _round(max(unit["pre_mean"] for unit in treated_units)),
            ],
            "control_pool_pre_range": None,
            "admissible_control_count": 0,
            "common_support": False,
            "note": "已获批或已到账的同地区商户被排除；不把受扶持商户当作未干预对照，也不跨地区补足。",
        }
        return cohort_record, None

    bounds = _support_bounds(treated_units)
    admissible = _admissible(control_units, bounds[:2])
    support = {
        "treated_pre_range": [
            _round(min(unit["pre_mean"] for unit in treated_units)),
            _round(max(unit["pre_mean"] for unit in treated_units)),
        ],
        "control_pool_pre_range": [
            _round(min(unit["pre_mean"] for unit in control_units)),
            _round(max(unit["pre_mean"] for unit in control_units)),
        ],
        "admissible_pre_range": [
            _round(min((unit["pre_mean"] for unit in admissible), default=None)),
            _round(max((unit["pre_mean"] for unit in admissible), default=None)),
        ],
        "admissible_share": _round(len(admissible) / len(control_units)) if control_units else None,
        "margin": _round(bounds[2]),
        "common_support": len(admissible) >= MIN_GROUP_SIZE,
    }
    cohort_record["admissible_control_count"] = len(admissible)
    cohort_record["support"] = support
    if len(admissible) < MIN_GROUP_SIZE:
        cohort_record["reason"] = "no_common_support_in_same_region_controls"
        return cohort_record, None

    treated_rows = [_row(unit, True) for unit in treated_units]
    control_rows = [_row(unit, False) for unit in admissible]
    primary = _fit(treated_rows + control_rows, adjust_trend=True)
    level = _fit(treated_rows + control_rows, adjust_trend=False)
    raw_estimate = _mean([unit["delta"] for unit in treated_units]) - _mean(
        [unit["delta"] for unit in admissible]
    )
    if primary is None:
        cohort_record["reason"] = "singular_regression_design"
        return cohort_record, None
    seed = study.study_key + "|" + cohort_id
    draws = _bootstrap_estimate(treated_rows, control_rows, True, BOOTSTRAP_REPLICATES, seed + "|boot")
    interval, interval_method = _interval(draws, primary["estimate"], primary["se"])

    # 安慰剂检查需要“该批次已识别”的视图，因此在做检查前先填入状态。
    identified_view = dict(cohort_record, status="estimated")
    pretrend = _pretrend_check(treated_units, admissible, seed + "|pretrend")
    diagnostics: dict[str, Any] = {}
    placebo = _placebo_check(study, identified_view, seed, diagnostics)
    influence = _influence_check(study, cohort_record, treated_units, admissible, primary["estimate"], seed)

    issues: list[str] = []
    if pretrend["pass"] is False:
        issues.append("pre_trend_differs")
    if placebo["pass"] is False:
        issues.append("placebo_nonzero")
    if placebo["pass"] is None:
        issues.append("placebo_unavailable")
    if not influence["influence_pass"]:
        issues.append("influence_sensitive")
    if not influence["concentration_pass"]:
        issues.append("concentrated_in_large_merchants")
    if issues:
        if "pre_trend_differs" in issues or "placebo_nonzero" in issues or "placebo_unavailable" in issues:
            strength = "weakened"
        else:
            strength = "average_only"
    else:
        strength = "supported"

    effect = {
        "cohort": cohort_id,
        "estimate": _round(primary["estimate"]),
        "interval": interval,
        "interval_method": interval_method,
        "status": "estimated",
        "strength": strength,
        "claim": _claim(strength),
        "estimator": PRIMARY_ESTIMATOR,
        "n_treated": len(treated_units),
        "n_controls": len(admissible),
        "sensitivity": {
            "raw_did": _round(raw_estimate),
            "level_adjusted_did": _round(level["estimate"]) if level else None,
            "trend_adjusted_did": _round(primary["estimate"]),
            "hc1_standard_error": _round(primary["se"]),
        },
        "checks": {
            "pre_trend_pass": pretrend["pass"],
            "placebo_pass": placebo["pass"],
            "influence_pass": influence["influence_pass"],
            "concentration_pass": influence["concentration_pass"],
        },
    }
    cohort_record["status"] = "estimated"
    cohort_record["reason"] = None
    cohort_record["strength"] = strength
    cohort_record["estimator"] = PRIMARY_ESTIMATOR
    extra = {
        "pretrend": pretrend,
        "placebo": placebo,
        "influence": influence,
        "diagnostic_placebo_runs": diagnostics.get("placebo_runs", {}),
        "bootstrap_draws": draws,
        "treated_units": treated_units,
        "control_units": admissible,
    }
    return cohort_record, {"effect": effect, "diagnostics": extra}


def _claim(strength: str) -> str:
    return {
        "supported": "在可比同地区商户中支持该批次平均销售提升的因果解释。",
        "partial": "结论只适用于已识别批次；未识别批次既不参与总体，也不视为零效果，不能据此表述为整个政策普遍有效。",
        "average_only": "平均效应方向稳定，但效应集中于少数大商户；只能表述为平均效应，不能表述为普遍有效。",
        "weakened": "干预前趋势或安慰剂检查未通过，不能给出确定的因果结论；数字仅作描述性参考。",
        "unidentified": "缺少同地区未受扶持对照，无法识别因果效应；该批次不视为零效果。",
    }[strength]


def build_study(
    payload: Mapping[str, Any],
    input_path: str | None = None,
    prepared: _Prepared | None = None,
) -> dict[str, Any]:
    study = prepared or _Prepared(payload)
    cohorts: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    pretrend_by_cohort: dict[str, Any] = {}
    placebo_by_cohort: dict[str, Any] = {}
    influence_by_cohort: dict[str, Any] = {}
    support_by_cohort: dict[str, Any] = {}
    placebo_runs: dict[str, Any] = {}
    identified_estimates: list[tuple[Mapping[str, Any], list[float], float]] = []

    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for merchant in study.merchants:
        if merchant not in study.first_paid:
            continue
        paid = study.first_paid[merchant]
        approved = study.first_approval.get(merchant, paid)
        payout_index = study.week_index(paid)
        if payout_index is None:
            continue
        payout_week = study.weeks[payout_index]
        grouped[(payout_week, study.region.get(merchant, "unknown"), approved.isoformat())].append(merchant)

    ordered_cohorts = sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2]))
    payout_counts: dict[str, int] = defaultdict(int)
    for (payout_week, _region, _approved), _members in ordered_cohorts:
        payout_counts[payout_week] += 1
    cohort_ids: dict[tuple[str, str, str], str] = {}
    for (payout_week, region, approved_text), _members in ordered_cohorts:
        suffix = "" if payout_counts[payout_week] == 1 else "-" + region
        cohort_ids[(payout_week, region, approved_text)] = "cohort-" + payout_week + suffix

    for (payout_week, region, approved_text), members in ordered_cohorts:
        cohort_id = cohort_ids[(payout_week, region, approved_text)]
        cohort_record, result = _cohort_result(
            study,
            cohort_id,
            region,
            date.fromisoformat(approved_text),
            date.fromisoformat(payout_week),
            members,
        )
        cohorts.append(cohort_record)
        support_by_cohort[cohort_id] = cohort_record["support"]
        if result is None:
            effects.append(
                {
                    "cohort": cohort_id,
                    "estimate": None,
                    "interval": None,
                    "status": "not_identified",
                    "strength": "unidentified",
                    "claim": _claim("unidentified"),
                    "reason": cohort_record["reason"],
                    "n_treated": cohort_record["treated_count"],
                    "n_controls": 0,
                }
            )
            pretrend_by_cohort[cohort_id] = {
                "status": "not_run",
                "reason": "cohort_not_identified",
                "pass": None,
            }
            placebo_by_cohort[cohort_id] = {
                "status": "not_run",
                "reason": "cohort_not_identified",
                "pass": None,
            }
            influence_by_cohort[cohort_id] = {
                "status": "not_run",
                "reason": "cohort_not_identified",
                "pass": None,
            }
            continue
        effect = result["effect"]
        effects.append(effect)
        extra = result["diagnostics"]
        pretrend_by_cohort[cohort_id] = extra["pretrend"]
        placebo_by_cohort[cohort_id] = extra["placebo"]
        influence_by_cohort[cohort_id] = extra["influence"]
        placebo_runs.update(extra["diagnostic_placebo_runs"])
        draws = extra["bootstrap_draws"]
        if len(draws) >= max(50, BOOTSTRAP_REPLICATES // 4):
            # 用确定性的 HC1 方差做总体权重，避免再用 bootstrap 方差估计权重
            # 导致“同一数据加常数平移”改变总体点估计。
            standard_error = float(effect["sensitivity"]["hc1_standard_error"] or 0.0)
            variance = standard_error ** 2
            weight = 1.0 / variance if variance > 1e-12 else 1.0
            identified_estimates.append((effect, draws, weight))

    overall = _overall_effect(identified_estimates, len(cohorts))
    effects = [overall] + effects
    diagnostics = _diagnostics(
        study,
        cohorts,
        pretrend_by_cohort,
        placebo_by_cohort,
        influence_by_cohort,
        support_by_cohort,
        placebo_runs,
    )
    limitations = _limitations(study, cohorts, effects[1:], diagnostics)
    source_versions = {
        "code": CODE_VERSION,
        "as_of": study.as_of.isoformat(),
        "input_path": input_path,
        "panel": {
            "rows_total": study.panel_rows_total,
            "rows_visible_at_as_of": study.panel_rows_visible,
            "merchants": len(study.merchants),
            "weeks": len(study.weeks),
            "first_week": study.weeks[0],
            "last_week": study.weeks[-1],
            "full_sha256": hashlib.sha256(stable_json(payload.get("panel", [])).encode("utf-8")).hexdigest(),
        },
        "payments": {
            "records_total": study.payment_rows_total,
            "records_visible_at_as_of": study.payment_rows_visible,
            "effective_payment_ids": len(study.payments),
            "late_revisions_ignored": len(study.payments_late_revisions),
            "visible_sha256": hashlib.sha256(
                stable_json(sorted(study.payments.items())).encode("utf-8")
            ).hexdigest(),
        },
        "analysis_sha256": study.analysis_fingerprint,
        "study_key": study.study_key,
        "parameters": PARAMETERS,
        "estimator": PRIMARY_ESTIMATOR,
        "generated_at": utc_now(),
    }
    study_id = "study-" + study.study_key[:24]
    return {
        "study_id": study_id,
        "as_of": study.as_of.isoformat(),
        "estimand": _estimand(),
        "cohorts": cohorts,
        "effects": effects,
        "diagnostics": diagnostics,
        "limitations": limitations,
        "source_versions": source_versions,
        "design": {
            "name": "stepped rollout difference-in-differences",
            "estimator": PRIMARY_ESTIMATOR,
            "treatment_timing": "first effective payout week; approval and eligibility are not treatment",
            "control_rule": "same pre_region, no approval and no payout through the cohort post window",
            "control_covariates": ["pre_region", "pre-period mean sales level (common support only)"],
            "forbidden_covariates": ["post_staff", "any post-payout sales", "final sales used for selection"],
            "windows": {
                "pre": "8 weeks immediately before the approval week",
                "post": "8 weeks starting at the payout week",
            },
            "parameters": PARAMETERS,
        },
    }


def _overall_effect(
    identified: Sequence[tuple[Mapping[str, Any], Sequence[float], float]],
    total_cohorts: int,
) -> dict[str, Any]:
    if not identified:
        return {
            "cohort": "overall",
            "estimate": None,
            "interval": None,
            "status": "not_identified",
            "strength": "unidentified",
            "claim": _claim("unidentified"),
            "cohorts_identified": 0,
            "cohorts_total": total_cohorts,
            "reason": "no_cohort_with_common_support",
        }
    weight_sum = sum(weight for _effect, _draws, weight in identified)
    normalized = [weight / weight_sum for _effect, _draws, weight in identified]
    point = sum(
        float(effect["estimate"]) * weight for (effect, _draws, _weight), weight in zip(identified, normalized)
    )
    replicate_count = min(len(draws) for _effect, draws, _weight in identified)
    combined = [
        sum(
            float(draws[index]) * weight
            for (_effect, draws, _weight), weight in zip(identified, normalized)
        )
        for index in range(replicate_count)
    ]
    interval = None
    if len(combined) >= max(50, BOOTSTRAP_REPLICATES // 4):
        ordered = sorted(combined)
        interval = [_round(_percentile(ordered, ALPHA / 2)), _round(_percentile(ordered, 1 - ALPHA / 2))]
    strength_rank = max(STRENGTH_ORDER[effect["strength"]] for effect, _draws, _weight in identified)
    if len(identified) < total_cohorts:
        # 有批次未被识别时，总体结论最多只能到 partial，不能读成整个政策普遍有效。
        strength_rank = max(strength_rank, STRENGTH_ORDER["partial"])
    strength = next(name for name, rank in STRENGTH_ORDER.items() if rank == strength_rank)
    return {
        "cohort": "overall",
        "estimate": _round(point),
        "interval": interval,
        "interval_method": "inverse_variance_weighted_bootstrap",
        "status": "estimated" if len(identified) == total_cohorts else "estimated_partial",
        "strength": strength,
        "claim": _claim(strength),
        "cohorts_identified": len(identified),
        "cohorts_total": total_cohorts,
        "cohort_weights": {
            effect["cohort"]: _round(weight)
            for (effect, _draws, _weight), weight in zip(identified, normalized)
        },
        "note": "未识别批次不进入总体，也不按零效应处理。",
    }


def _diagnostics(
    study: _Prepared,
    cohorts: Sequence[Mapping[str, Any]],
    pretrend: Mapping[str, Any],
    placebo: Mapping[str, Any],
    influence: Mapping[str, Any],
    support: Mapping[str, Any],
    placebo_runs: Mapping[str, Any],
) -> dict[str, Any]:
    post_staff_changes = _post_staff_changes(study)
    identified = [cohort for cohort in cohorts if cohort["status"] == "estimated"]
    return {
        "as_of": study.as_of.isoformat(),
        "sample_flow": _sample_flow(study, cohorts),
        "support": support,
        "pretrend": {
            "by_cohort": dict(pretrend),
            "test": "permutation on mean pre-period slopes; pass when p >= alpha",
            "alpha": ALPHA,
        },
        "placebo": {
            "by_cohort": dict(placebo),
            "runs": dict(placebo_runs),
            "design": "fake payout shifted 8 weeks earlier with 4-week pre and 4-week post windows",
        },
        "influence": {
            "by_cohort": dict(influence),
            "drop_rule": "drop up to 3 largest treated merchants by pre-period mean sales",
            "limits": {
                "max_relative_change": INFLUENCE_DROP_RATIO,
                "max_top_decile_share": CONCENTRATION_LIMIT,
            },
        },
        "post_treatment_features": {
            "post_staff": {
                "used_for_control_selection": False,
                "used_in_estimator": False,
                "changes_at_payout": post_staff_changes["changing_merchants"],
                "treated_changes_at_payout": post_staff_changes["treated_changes"],
                "control_changes": post_staff_changes["control_changes"],
                "note": "人员数在到账周才变化，属于政策之后形成的特征，只作诊断，不参与匹配或回归。",
            }
        },
        "revisions": {
            "late_revisions_ignored": list(study.payments_late_revisions),
            "duplicate_revision_rows_collapsed": len(study.duplicate_reports),
            "exposure_changes": list(study.exposure_changes),
            "rule": "只使用 known_at <= as_of 的最高修订版本；迟到更正不改变旧研究。",
        },
        "identified_cohorts": [cohort["cohort"] for cohort in identified],
        "unidentified_cohorts": [
            {"cohort": cohort["cohort"], "reason": cohort["reason"]}
            for cohort in cohorts
            if cohort["status"] != "estimated"
        ],
    }


def _sample_flow(study: _Prepared, cohorts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    categories = defaultdict(int)
    for merchant in study.merchants:
        categories[study.category(merchant)] += 1
    return {
        "panel_rows_total": study.panel_rows_total,
        "panel_rows_visible_at_as_of": study.panel_rows_visible,
        "merchants_panel": len(study.merchants),
        "merchants_treated": categories.get("treated", 0),
        "merchants_exited_by_reversal": categories.get("exited_by_reversal", 0),
        "merchants_approved_not_paid_excluded": categories.get("approved_not_paid", 0),
        "merchants_never_approved_control_candidates": categories.get("control_candidate", 0),
        "payment_records_total": study.payment_rows_total,
        "payment_records_visible_at_as_of": study.payment_rows_visible,
        "payment_records_late_ignored": len(study.payments_late_revisions),
        "cohorts_total": len(cohorts),
        "cohorts_estimated": sum(1 for cohort in cohorts if cohort["status"] == "estimated"),
        "cohorts_not_identified": sum(1 for cohort in cohorts if cohort["status"] != "estimated"),
        "cohort_sizes": {cohort["cohort"]: cohort["treated_count"] for cohort in cohorts},
        "cohort_control_pool": {cohort["cohort"]: cohort["control_pool_size"] for cohort in cohorts},
    }


def _post_staff_changes(study: _Prepared) -> dict[str, Any]:
    """只读取 post_staff 以展示它是干预后特征；不参与对照选择或估计。"""
    panel = study.payload.get("panel", [])
    rows: dict[tuple[str, str], Any] = {}
    for record in panel:
        if not isinstance(record, Mapping) or not _visible(record, study.as_of):
            continue
        merchant = str(record.get("merchant_id", "")).strip()
        week = str(record.get("week", "")).strip()
        rows[(merchant, week)] = record.get("post_staff")
    weeks = study.weeks
    changing: list[str] = []
    transitions: dict[str, str] = {}
    for merchant in study.merchants:
        values = [rows.get((merchant, week)) for week in weeks]
        previous = None
        for index, value in enumerate(values):
            if previous is not None and value != previous:
                transitions[merchant] = weeks[index]
                break
            previous = value
        if merchant in transitions:
            changing.append(merchant)
    treated_changes = 0
    control_changes = 0
    for merchant in changing:
        if merchant in study.first_paid:
            paid_week = study.week_index(study.first_paid[merchant])
            if paid_week is not None and transitions[merchant] == weeks[paid_week]:
                treated_changes += 1
        else:
            control_changes += 1
    return {
        "changing_merchants": len(changing),
        "treated_changes": treated_changes,
        "control_changes": control_changes,
        "transitions": transitions,
    }


def _limitations(
    study: _Prepared,
    cohorts: Sequence[Mapping[str, Any]],
    effects: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
) -> list[str]:
    limitations = [
        "这是非随机化的准实验：同一地区内未受扶持商户作为对照，仍可能存在未观测的、与批次相关的冲击。",
        "处理按首次有效到账定义；获批但未到账的商户被排除，不当作对照，也不当作处理。",
        "结果窗口为到账后 8 周，不能外推到更长期或溢出效应；地区内对照可能受同一市场的溢出影响。",
        "对照筛选与回归只使用干预前信息；post_staff 在到账周才变化，属于干预后特征，未参与匹配或估计。",
        "所有区间来自商户层面聚类 bootstrap（{0} 次），样本较小时区间宽度可能被低估。".format(BOOTSTRAP_REPLICATES),
    ]
    unidentified = [cohort for cohort in cohorts if cohort["status"] != "estimated"]
    for cohort in unidentified:
        limitations.append(
            "批次 {0} 无法识别（{1}）：没有同地区、窗口内未获批且未到账的对照；该批次不进入总体，也不按零效果处理。".format(
                cohort["cohort"], cohort["reason"]
            )
        )
    weakened = [effect for effect in effects if effect["status"] == "estimated" and effect["strength"] == "weakened"]
    average_only = [effect for effect in effects if effect["status"] == "estimated" and effect["strength"] == "average_only"]
    if weakened:
        limitations.append(
            "批次 {0} 的干预前趋势或安慰剂检查未通过，相关数字降级为描述性，不支撑因果结论。".format(
                ", ".join(effect["cohort"] for effect in weakened)
            )
        )
    if average_only:
        limitations.append(
            "批次 {0} 的效应集中于少数大商户，不能把平均结果表述为普遍有效。".format(
                ", ".join(effect["cohort"] for effect in average_only)
            )
        )
    if diagnostics["revisions"]["late_revisions_ignored"]:
        limitations.append(
            "分析截止 {0} 之后才得知的 {1} 条支付修订未参与本次研究；若其改变处理状态，需要新研究而不是改写旧研究。".format(
                study.as_of.isoformat(), len(diagnostics["revisions"]["late_revisions_ignored"])
            )
        )
    return limitations


def _estimand() -> dict[str, Any]:
    return {
        "name": "cohort_average_treatment_effect_on_paid_merchants",
        "type": "cohort-specific ATT under stepped rollout",
        "target_population": "首次有效到账周属于该批次的商户",
        "treatment": "首次有效到账（paid_at）开始的政策扶持暴露",
        "outcome": "商户周销售额（元）在到账后 8 周均值相对获批前 8 周均值的变化",
        "contrast": "同一经营地区内、到账窗口结束前未获批且未到账的商户",
        "estimator": PRIMARY_ESTIMATOR,
        "identification_assumption": [
            "在给定干预前销售水平和干预前斜率后，处理组与同地区对照组的销售变化趋势相同（平行趋势）。",
            "对照商户在研究窗口内未被同一政策覆盖，且不因预期到账而改变行为。",
            "处理批次按到账周外生分配，或分配只依赖干预前特征。",
            "无共同支持的批次不被强行估计；其效应不可识别。",
        ],
        "unit": "yuan per merchant-week",
        "notes": "区间与检验按商户聚类；总体效应只在可识别批次上做逆方差加权。",
    }


# ---------------------------------------------------------------------------
# 持久化：只新增研究表，不修改原有业务表
# ---------------------------------------------------------------------------

def persist_study(
    database: str | Path,
    result: Mapping[str, Any],
    payment_refs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    ledger = SQLiteLedger(database)
    study_id = str(result["study_id"])
    links: list[tuple[str, str, str]] = []
    payment_refs = payment_refs or {}
    for cohort in result.get("cohorts", []):
        links.append(("cohort", str(cohort["cohort"]), stable_json({
            "payout_week": cohort.get("payout_week"),
            "region": cohort.get("region"),
            "status": cohort.get("status"),
        })))
        treated = set(cohort.get("treated_ids", []))
        for payment_id in sorted(payment_refs):
            record = payment_refs[payment_id]
            if record["merchant_id"] in treated:
                links.append((
                    "payment_record",
                    payment_id,
                    stable_json({
                        "merchant_id": record["merchant_id"],
                        "approved_at": record["approved_at"],
                        "paid_at": record["paid_at"],
                        "cohort": cohort["cohort"],
                    }),
                ))
        for merchant in sorted(treated):
            links.append((
                "merchant",
                merchant,
                stable_json({"cohort": cohort["cohort"], "region": cohort.get("region")}),
            ))
    with ledger.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rollout_studies(
                study_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                code_version TEXT NOT NULL,
                study_key TEXT NOT NULL,
                estimand TEXT NOT NULL,
                cohorts TEXT NOT NULL,
                effects TEXT NOT NULL,
                diagnostics TEXT NOT NULL,
                limitations TEXT NOT NULL,
                source_versions TEXT NOT NULL,
                output TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rollout_study_links(
                study_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(study_id, link_type, entity_id)
            );
            """
        )
        original_runs = _matching_original_runs(connection, result)
        for run_id, merchant_id in original_runs:
            links.append((
                "original_run",
                run_id,
                stable_json({"merchant_id": merchant_id, "note": "reference only; original approval unchanged"}),
            ))
        now = utc_now()
        connection.execute(
            """
            INSERT INTO rollout_studies(
                study_id, as_of, code_version, study_key, estimand, cohorts, effects,
                diagnostics, limitations, source_versions, output, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(study_id) DO UPDATE SET
                output=excluded.output,
                diagnostics=excluded.diagnostics,
                created_at=excluded.created_at
            """,
            (
                study_id,
                result["as_of"],
                CODE_VERSION,
                result["source_versions"]["study_key"],
                stable_json(result["estimand"]),
                stable_json(result["cohorts"]),
                stable_json(result["effects"]),
                stable_json(result["diagnostics"]),
                stable_json(result["limitations"]),
                stable_json(result["source_versions"]),
                stable_json(result),
                now,
            ),
        )
        for link_type, entity_id, detail in links:
            connection.execute(
                "INSERT OR IGNORE INTO rollout_study_links(study_id, link_type, entity_id, detail, created_at) VALUES(?,?,?,?,?)",
                (study_id, link_type, entity_id, detail, now),
            )
    with ledger.connect() as connection:
        counts = {
            "rollout_studies": int(connection.execute("SELECT COUNT(*) FROM rollout_studies").fetchone()[0]),
            "rollout_study_links": int(connection.execute("SELECT COUNT(*) FROM rollout_study_links").fetchone()[0]),
            "business_runs": int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        }
        link_types = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT link_type FROM rollout_study_links WHERE study_id=?", (study_id,)
            )
        ]
        linked_original_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM rollout_study_links WHERE study_id=? AND link_type='original_run'",
                (study_id,),
            ).fetchone()[0]
        )
    return {
        "storage": counts,
        "link_types": sorted(link_types),
        "linked_original_runs": linked_original_runs,
    }


def _matching_original_runs(connection: Any, result: Mapping[str, Any]) -> list[tuple[str, str]]:
    """只读引用原业务审批记录；不更新、不删除。"""
    merchants = set()
    for cohort in result.get("cohorts", []):
        merchants.update(str(merchant) for merchant in cohort.get("treated_ids", []))
    matches: list[tuple[str, str]] = []
    try:
        rows = connection.execute("SELECT run_id, payload FROM runs").fetchall()
    except Exception:
        return matches
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        merchant = str(payload.get("merchant_id", ""))
        if merchant in merchants:
            matches.append((str(row["run_id"]), merchant))
    return matches


def run_study(input_path: str | Path, database: str | Path, output_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(input_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    prepared = _Prepared(payload)
    result = build_study(payload, input_path=str(path), prepared=prepared)
    storage = persist_study(database, result, prepared.effective_payment_refs)
    result["storage"] = storage
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return result
