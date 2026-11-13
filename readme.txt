扶持政策业务研究服务。现有资格、申请、配额、同业摘要和效果记录形成一个可追踪的政策处理流程。

环境：Linux，Python 3.11.2 或更新的兼容版本。所有依赖来自 Python 标准库，无需安装第三方包。

测试：python3 -m unittest discover -s tests/policy_trials -p 'test_*.py'
构建：python3 -m compileall -q policy_trial_evaluator tests/policy_trials
验收：python3 -m policy_trial_evaluator.review.cli fixtures/demo.json --database /tmp/policy_trial_evaluator-acceptance.sqlite3
所有输入均为本地合成数据，运行不需要网络。

目录：policy_trial_evaluator 保存业务处理与存储模块；tests/policy_trials 保存业务回归；fixtures/demo.json 为原有业务样例，fixtures/analysis 为经营分析资料及字段说明。

原有验收命令每次使用新的 SQLite 路径，避免复用上次状态。新增输入完整性检查：
python3 -m unittest discover -s tests/analysis_inputs -v

分批发放研究（与原有审批互不影响，只新增研究表）：
python3 -m policy_trial_evaluator.rollout_study --input fixtures/analysis/input.json --database /tmp/rollout-study.sqlite3 --output /tmp/rollout-study.json
输出 study_id、estimand、cohorts、effects、diagnostics、limitations、source_versions；每个效果含 cohort、estimate、interval、status。方法说明见 policy_trial_evaluator/studies/rollout.py 顶部与输出中的 design/estimand 字段。

容器运行可使用已经预装的 ccusr/prepared:3f007a95fcee-4441968e5ff6 镜像；将当前仓库只读挂载至 /source，在容器内复制到 /tmp/product 后运行上述命令。依赖准备和正式运行分离，运行过程中不需要网络访问。
