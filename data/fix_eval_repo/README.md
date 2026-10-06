# fix_eval_repo —— 修复引擎评测夹具仓库

本目录是 `eval_fix.py`（P1-5 评测扩样）的**离线评测夹具仓库**，独立于 `demo-app`。
demo-app 是活体演示基线（沙箱必须保持全绿），不适合注入 30+ 缺陷；本仓库以独立目录
承载 31 个批量注入缺陷 + 2 个并库缺陷，共支撑 33 个评测样本，使 fix 评测样本量达到
统计置信度要求。

## 结构

- 并库模块（与 demo-app 基线一致）：
  - `order_service.py` + `coupon_client.py` + `order_repository.py` —— F01 空券解引用
  - `auth/token_service.py` —— F02 畸形令牌裸 ValueError
- 扩展缺陷模块（9 域 31 个缺陷，每个缺陷以行内注释 `缺陷 XN：` 标注，docstring 声明正确语义）：
  - `billing/invoice.py`（A1-A4：档位边界 / 浮点四舍五入 / 除零 / 可选字段缺省）
  - `inventory/stock.py`（B1-B4：负库存 / 可变默认参数 / 批次漏算 / 环境变量类型）
  - `pricing/discount.py`（C1-C4：负数下限 / 百分比截断 / 空列表 / 叠加顺序）
  - `resilience/retry.py`（D1-D3：次数 off-by-one / 异常掩盖 / 单位错误）
  - `parsing/parser.py`（E1-E4：键空白 / 分钟换算 / 编码 / 贪婪正则）
  - `storage/kv.py`（F1-F3：覆盖写入 / 类型转换 / 空键校验）
  - `scheduling/window.py`（G1-G3：开区间 / 换算系数 / 负数校验）
  - `stats/aggregate.py`（H1-H3：空均值 / 偶数中位数 / 百分位越界）
  - `cache/lru.py`（I1-I3：None 值判定 / 读取刷新 / 过早淘汰）

## 基线

- 全部 52 个用例中恰好 32 个失败（24 failures + 8 errors），其余 20 个通过用例作回归保护；
- 每个失败用例恰好对应一个评测样本（F01 + 扩展 31 个）；F02 无目标用例，
  验收口径为「不引入回归」；
- 基线由 `scripts/gen_fix_eval_samples.py` 实时采集（不存静态快照），样本的
  `baseline_failed_tests = 全部失败用例 - 本人目标用例`。

## 约束

- 仅使用标准库（沙箱镜像 python:3.12-slim 无第三方依赖）；
- 测试方法名全局唯一（沙箱仅按裸方法名解析失败用例）；
- 样本的 alert.description 不得包含演示分支标记词（low-conf / protected / test-fail 等），
  否则 fix_agent 会走确定性短路分支；
- 本目录不参与 demo-app 实时演示链路，仅供 `eval_fix.py` 离线评测使用。
