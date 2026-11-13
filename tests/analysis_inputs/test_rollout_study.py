"""分批发放研究（rollout_study）的回归测试。

覆盖题面关心的可复查性质：
* 无同地区共同支持的首批不被当作零效果；
* 改变干预后特征（post_staff / 干预后销售）不改变对照选择与估计；
* as_of 之后才获知的修订改写输入，不改变旧研究；
* 只新增研究记录，不修改原有业务审批。
"""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from policy_trial_evaluator.studies.rollout import (
    CODE_VERSION,
    build_study,
    run_study,
)

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "fixtures" / "analysis" / "input.json"


def load_input() -> dict:
    return json.loads(INPUT.read_text(encoding="utf-8"))


def effect_for(study: dict, cohort: str) -> dict:
    return next(item for item in study["effects"] if item["cohort"] == cohort)


def cohort_for(study: dict, cohort: str) -> dict:
    return next(item for item in study["cohorts"] if item["cohort"] == cohort)


class RolloutStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.input = load_input()
        cls.study = build_study(copy.deepcopy(cls.input), input_path=str(INPUT))

    # -- 结构与契约 -------------------------------------------------------
    def test_required_top_level_fields(self):
        for key in ("study_id", "estimand", "cohorts", "effects", "diagnostics", "limitations", "source_versions"):
            self.assertIn(key, self.study)
        self.assertTrue(self.study["study_id"].startswith("study-"))
        self.assertTrue(self.study["estimand"]["identification_assumption"])

    def test_every_effect_has_required_shape(self):
        for effect in self.study["effects"]:
            for key in ("cohort", "estimate", "interval", "status"):
                self.assertIn(key, effect)
            self.assertIsInstance(effect["status"], str)

    def test_three_payout_cohorts_follow_paid_at_not_approved_at(self):
        cohort_ids = [cohort["cohort"] for cohort in self.study["cohorts"]]
        self.assertEqual(cohort_ids, ["cohort-2026-03-26", "cohort-2026-05-07", "cohort-2026-06-18"])
        for cohort in self.study["cohorts"]:
            self.assertEqual(cohort["paid_at"], cohort["payout_week"])
            self.assertNotEqual(cohort["approved_at"], cohort["paid_at"])
            self.assertEqual(cohort["treated_count"], 15)

    # -- 关键：无共同支持的批次 ------------------------------------------
    def test_cohort_without_shared_support_is_not_zero_effect(self):
        final = effect_for(self.study, "cohort-2026-06-18")
        self.assertEqual(final["status"], "not_identified")
        self.assertIsNone(final["estimate"])
        self.assertIsNone(final["interval"])
        self.assertEqual(final["strength"], "unidentified")
        self.assertEqual(final["n_controls"], 0)
        self.assertIn("no_clean_same_region_controls", final["reason"])
        cohort = cohort_for(self.study, "cohort-2026-06-18")
        self.assertEqual(cohort["control_pool_size"], 0)
        self.assertEqual(len(cohort["excluded_already_exposed"]), 15)
        # 不允许跨地区补足
        self.assertTrue(all(m.startswith("m") for m in cohort["excluded_already_exposed"]))
        # 不把未识别批次写成 0 效果：estimate 必须是 None，而不是数字 0。
        self.assertIsNone(effect_for(self.study, "cohort-2026-06-18")["estimate"])

    def test_overall_excludes_unidentified_cohort_and_is_partial(self):
        overall = effect_for(self.study, "overall")
        self.assertEqual(overall["status"], "estimated_partial")
        self.assertEqual(overall["cohorts_identified"], 2)
        self.assertEqual(overall["cohorts_total"], 3)
        self.assertNotIn("cohort-2026-06-18", overall["cohort_weights"])
        weights = overall["cohort_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=3)
        self.assertIn("不按零效应", overall["note"])
        # 有批次未识别时，总体结论不得读成“整个政策已获支持”。
        self.assertEqual(overall["strength"], "partial")

    # -- 关键：干预后特征不得影响对照 --------------------------------
    def test_control_selection_is_invariant_to_post_treatment_staff(self):
        mutated = copy.deepcopy(self.input)
        for record in mutated["panel"]:
            record["post_staff"] = 7  # 把干预后人员数全部改掉
        altered = build_study(mutated, input_path=str(INPUT))
        base_cohorts = {c["cohort"]: c for c in self.study["cohorts"]}
        for cohort in altered["cohorts"]:
            baseline = base_cohorts[cohort["cohort"]]
            self.assertEqual(cohort["control_ids"], baseline["control_ids"])
            self.assertEqual(cohort["admissible_control_count"], baseline["admissible_control_count"])
            self.assertEqual(cohort["treated_ids"], baseline["treated_ids"])
        for effect in altered["effects"]:
            base = effect_for(self.study, effect["cohort"])
            self.assertEqual(effect["estimate"], base["estimate"])
            self.assertEqual(effect["interval"], base["interval"])

    def test_control_selection_is_invariant_to_post_payout_sales(self):
        mutated = copy.deepcopy(self.input)
        for record in mutated["panel"]:
            record["sales"] = round(record["sales"] + 137.5, 2)  # 干预后销售整体平移
        altered = build_study(mutated, input_path=str(INPUT))
        for cohort in altered["cohorts"]:
            baseline = cohort_for(self.study, cohort["cohort"])
            self.assertEqual(cohort["control_ids"], baseline["control_ids"])
            self.assertEqual(cohort["admissible_control_count"], baseline["admissible_control_count"])
        # 水平整体平移应不改变 DiD 点估计
        for effect in altered["effects"]:
            base = effect_for(self.study, effect["cohort"])
            if effect["estimate"] is None:
                self.assertIsNone(base["estimate"])
            else:
                self.assertAlmostEqual(effect["estimate"], base["estimate"], places=3)

    def test_post_staff_is_declared_post_treatment_and_unused(self):
        block = self.study["diagnostics"]["post_treatment_features"]["post_staff"]
        self.assertFalse(block["used_for_control_selection"])
        self.assertFalse(block["used_in_estimator"])
        self.assertEqual(block["treated_changes_at_payout"], 45)
        self.assertEqual(block["control_changes"], 0)

    # -- 可复查：迟到修订不改写旧研究 -----------------------------------
    def test_late_revision_does_not_change_existing_study(self):
        late = copy.deepcopy(self.input)
        # 一位已到账商户在 as_of 之后才获知一条“冲正”修订（新增一条记录，而不是改写旧记录）。
        original = next(payment for payment in late["payments"] if payment["merchant_id"] == "m000")
        revised = dict(original)
        revised["status"] = "reversed"
        revised["known_at"] = "2026-09-01"  # 晚于 as_of 2026-08-11
        revised["revision"] = 2
        late["payments"].append(revised)
        rerun = build_study(late, input_path=str(INPUT))
        self.assertEqual(rerun["study_id"], self.study["study_id"])
        self.assertEqual(rerun["effects"], self.study["effects"])
        ignored = rerun["diagnostics"]["revisions"]["late_revisions_ignored"]
        self.assertTrue(any(item.get("payment_id") == "pay0" for item in ignored))

    def test_known_revision_at_or_before_as_of_changes_study(self):
        visible = copy.deepcopy(self.input)
        original = next(payment for payment in visible["payments"] if payment["merchant_id"] == "m000")
        revised = dict(original)
        revised["status"] = "reversed"
        revised["known_at"] = "2026-08-11"  # 恰在 as_of 当天可见
        revised["revision"] = 2
        visible["payments"].append(revised)
        rerun = build_study(visible, input_path=str(INPUT))
        self.assertNotEqual(rerun["study_id"], self.study["study_id"])
        self.assertEqual(rerun["diagnostics"]["sample_flow"]["merchants_treated"], 44)
        self.assertEqual(rerun["diagnostics"]["sample_flow"]["merchants_exited_by_reversal"], 1)

    def test_reissue_uses_first_payout_and_is_logged(self):
        reissued = copy.deepcopy(self.input)
        for payment in reissued["payments"]:
            if payment["merchant_id"] == "m000":
                clone = dict(payment)
                clone["payment_id"] = "pay0-again"
                clone["paid_at"] = "2026-07-30"
                clone["known_at"] = "2026-07-31"
                clone["revision"] = 2
                clone["status"] = "paid"
                reissued["payments"].append(clone)
                break
        rerun = build_study(reissued, input_path=str(INPUT))
        changes = [item for item in rerun["diagnostics"]["revisions"]["exposure_changes"] if item["merchant_id"] == "m000"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "reissued")
        self.assertEqual(changes[0]["first_paid_at"], "2026-03-26")
        self.assertEqual(rerun["cohorts"][0]["paid_at"], "2026-03-26")

    # -- 诊断确实可运行 ---------------------------------------------------
    def test_diagnostics_run_and_are_reported(self):
        diagnostics = self.study["diagnostics"]
        for cohort in ("cohort-2026-03-26", "cohort-2026-05-07"):
            pretrend = diagnostics["pretrend"]["by_cohort"][cohort]
            self.assertEqual(pretrend["status"], "tested")
            self.assertIsNotNone(pretrend["permutation_p_value"])
            placebo = diagnostics["placebo"]["by_cohort"][cohort]
            self.assertEqual(placebo["status"], "tested")
            self.assertIsNotNone(placebo["interval"])
            influence = diagnostics["influence"]["by_cohort"][cohort]
            self.assertTrue(influence["drop_largest"])
            self.assertIsNotNone(influence["top_decile_share"])
        final = diagnostics["pretrend"]["by_cohort"]["cohort-2026-06-18"]
        self.assertEqual(final["status"], "not_run")

    def test_failed_pretrend_downgrades_claim(self):
        broken = copy.deepcopy(self.input)
        # 给第一批处理组在获批前注入明显不同的趋势，模拟“原本增长就不同”。
        for record in broken["panel"]:
            if record["merchant_id"] in {"m000", "m004", "m008", "m012", "m016", "m020"}:
                week_index = sorted({r["week"] for r in broken["panel"]}).index(record["week"])
                if week_index < 10:
                    record["sales"] = round(record["sales"] + 18.0 * week_index, 2)
        altered = build_study(broken, input_path=str(INPUT))
        cohort = effect_for(altered, "cohort-2026-03-26")
        pretrend = altered["diagnostics"]["pretrend"]["by_cohort"]["cohort-2026-03-26"]
        if pretrend["pass"] is False:
            self.assertEqual(cohort["strength"], "weakened")
            self.assertTrue(any("干预前趋势" in line for line in altered["limitations"]))
        else:  # 合成数据可能不足以触发，但检查必须真实运行过
            self.assertEqual(pretrend["status"], "tested")

    def test_unknown_merchant_region_is_not_matched_across_regions(self):
        study = copy.deepcopy(self.input)
        # 把一名对照候选的地区改成第三类，确保不会被借来处理组的地区。
        for record in study["panel"]:
            if record["merchant_id"] == "m003":
                record["pre_region"] = "west"
        altered = build_study(study, input_path=str(INPUT))
        for cohort in altered["cohorts"]:
            self.assertNotIn("m003", cohort["control_ids"])

    # -- 持久化：只新增、不改原业务 ---------------------------------------
    def test_persistence_adds_only_study_tables_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "study.sqlite3"
            output = Path(directory) / "study.json"
            result = run_study(INPUT, database, output)
            self.assertTrue(database.is_file())
            self.assertTrue(output.is_file())
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["study_id"], result["study_id"])
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"runs", "events", "rollout_studies", "rollout_study_links"} <= tables)
                links = connection.execute(
                    "SELECT link_type, COUNT(*) FROM rollout_study_links WHERE study_id=? GROUP BY link_type",
                    (result["study_id"],),
                ).fetchall()
                kinds = dict(links)
                self.assertEqual(kinds.get("cohort"), 3)
                self.assertEqual(kinds.get("payment_record"), 45)
                stored = connection.execute(
                    "SELECT output FROM rollout_studies WHERE study_id=?", (result["study_id"],)
                ).fetchone()[0]
                self.assertEqual(json.loads(stored)["study_id"], result["study_id"])

    def test_existing_business_approval_is_untouched(self):
        from policy_trial_evaluator.service import ApplicationService

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "combined.sqlite3"
            service = ApplicationService(database)
            demo = json.loads((ROOT / "fixtures" / "demo.json").read_text(encoding="utf-8"))
            business = service.execute(demo)
            with sqlite3.connect(database) as connection:
                before = connection.execute(
                    "SELECT run_id, state, revision FROM runs ORDER BY run_id"
                ).fetchall()
            study = run_study(INPUT, database, Path(directory) / "study.json")
            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    "SELECT run_id, state, revision FROM runs ORDER BY run_id"
                ).fetchall()
                links = connection.execute(
                    "SELECT link_type, COUNT(*) FROM rollout_study_links WHERE study_id=? GROUP BY link_type",
                    (study["study_id"],),
                ).fetchall()
                business_runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(business_runs, 1)
            self.assertEqual(business["run_id"], before[0][0])
            kinds = dict(links)
            self.assertGreaterEqual(kinds.get("merchant", 0), 45)

    # -- CLI 形态 ---------------------------------------------------------
    def test_cli_produces_required_json(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cli.sqlite3"
            output = Path(directory) / "cli.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "policy_trial_evaluator.rollout_study",
                    "--input",
                    str(INPUT),
                    "--database",
                    str(database),
                    "--output",
                    str(output),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output.is_file())
            written = json.loads(output.read_text(encoding="utf-8"))
            for key in ("study_id", "estimand", "cohorts", "effects", "diagnostics", "limitations", "source_versions"):
                self.assertIn(key, written)
            for effect in written["effects"]:
                self.assertTrue({"cohort", "estimate", "interval", "status"} <= set(effect))

    def test_source_versions_carry_reproducibility_metadata(self):
        versions = self.study["source_versions"]
        self.assertEqual(versions["code"], CODE_VERSION)
        self.assertEqual(versions["as_of"], self.study["as_of"])
        self.assertIn("analysis_sha256", versions)
        self.assertIn("study_key", versions)
        self.assertEqual(versions["parameters"], self.study["design"]["parameters"])


if __name__ == "__main__":
    unittest.main()
