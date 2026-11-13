"""`python3 -m policy_trial_evaluator.rollout_study` 命令行入口。"""

from __future__ import annotations

import argparse
import json

from policy_trial_evaluator.studies.rollout import run_study


def main() -> int:
    parser = argparse.ArgumentParser(description="分批发放政策扶持效果研究（可复查准实验）")
    parser.add_argument("--input", required=True, help="经营分析输入 JSON（panel + payments）")
    parser.add_argument("--database", required=True, help="研究记录 SQLite 路径（只新增研究表）")
    parser.add_argument("--output", required=True, help="研究结果 JSON 输出路径")
    args = parser.parse_args()
    result = run_study(args.input, args.database, args.output)
    summary = {
        "study_id": result["study_id"],
        "as_of": result["as_of"],
        "effects": [
            {
                "cohort": effect["cohort"],
                "estimate": effect["estimate"],
                "interval": effect["interval"],
                "status": effect["status"],
                "strength": effect["strength"],
            }
            for effect in result["effects"]
        ],
        "storage": result.get("storage"),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
