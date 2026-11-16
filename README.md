# 气候指标批次采集台账

本项目供研究团队离线接收不同机构交付的气候指标摘录。系统核验来源和指标契约，保留每次批次修订，记录质量证据，并发布不可变、可校验的快照。演示数据均为本地合成数据，不代表真实气候观测，也不在运行时访问外部网站。

使用 Python 3.11 或更新版本即可运行，无第三方依赖。以下命令均从项目根目录执行。

## 快速演示

运行 python -m climateledger demo --workspace fixtures，完成本地演示：创建 SQLite 数据库、注册合成指标与来源、摄入两行数据、发布快照并校验导出。再次执行会重用相同批次和发布版本。

单独使用时，把 --db path/to/ledger.sqlite3 放在子命令前。依次执行 register-indicator contract.json、register-source contract.json；需要单位换算时执行 register-unit rule.json。用 ingest batch.manifest.json 摄入数据，batch ID 查看记录；待审批次用 approve ID REVISION 审核，然后用 publish ID REVISION 发布。

export VERSION --output bundle.json 导出指定快照，verify bundle.json 检查摘要，series INDICATOR --version VERSION 查询发布值。coverage、discrepancies、compare、replay 和 audit 分别用于查看覆盖、来源分歧、版本变化、原始文件重放及审计链。conflicts INDICATOR 列出跨来源冲突，adjudicate INDICATOR PERIOD 记录人工裁决，external-series INDICATOR 生成单一对外序列，adjudications 查看裁决记录及其时效。

## 输入与状态

指标契约包含 key、title、canonical_unit 和 cadence（月份或年份）；可附带上下限、精度和负值许可。来源契约包含标识、标题、授权说明及允许提交的指标列表。相同标识下契约不可静默改写。单位规则采用“原值乘以 factor 加 offset”换算为规范单位。

批次清单指定来源、外部批次编号、同目录的 CSV 或 JSON 数据文件、文件 SHA-256、行数及四个字段的列映射：indicator、period、value、unit。文件仅从本地读取。非法行形成拒收证据；序列内部缺期和已发布值的变更需人工审核，不会被悄悄修正。相同字节重复提交幂等，内容变化形成修订。清单中的行数和摘要可检测不完整或被修改的输入。

发布版本包含上个版本的摘要、来源文件摘要、批次修订、当前测量集合及导出摘要。不同来源对同一指标和期间的观测保持独立。旧版本始终可以独立导出和验证。

## 冲突裁决与对外序列

两个授权机构可能就同一指标、同一期间各自交付观测。台账原样保留各来源的发布值，分来源导出（export、series --source）不受影响；交给研究团队的单一对外序列由 external-series 生成，取值只依据采集契约、质量证据和发布序列，绝不按来源名称碰运气：

- 可比较性由契约决定：观测必须来自已发布批次，期间符合指标 cadence，发布单位与契约 canonical_unit 一致（摄入时已按注册单位规则换算，原值与换算规则随批次留存）。不满足时该期间标记 not_comparable，只能人工裁决。
- 仅一家观测或多家规范化值一致时自动采用，依据分别为 single_source、values_agree，行内附带参与比较的批次。
- 多家值不一致即冲突。conflicts 列出参与比较的批次、原始值与单位、批次质量证据；未经裁决的冲突不会进入对外序列，而是列入 withheld（unresolved_conflict）。
- adjudicate INDICATOR PERIOD (--value 值 | --reject) --rationale 理由 记录人工裁决：所选值必须等于某条参与比较的观测，--reject 表示本期不发布权威值。裁决把契约快照、参与批次、质量证据一并存证并计算摘要，同时写入审计事件。
- 来源后续更正形成新发布版本后，若参与比较的观测集合发生变化，旧裁决因证据摘要不匹配而自动失效：adjudications 中标记 stale，conflicts 中标记 reopened，该期间重新 withheld（adjudication_stale），直至重新裁决。与该期间无关的发布不影响裁决效力。
- 对外序列的每个值都能追到参与比较的批次及裁决依据（裁决 id、理由、证据摘要）；历史版本的外部序列可按版本复现。

## 验证命令

python -m unittest discover -s tests -v

python -m compileall -q climateledger

python -m climateledger demo --workspace fixtures
