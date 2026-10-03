# demo-app（演示代码库）

AIOps 演示中的「被运维服务」代码库，供 Code RAG（WP4）做 AST 语义切块与向量索引：

- `order_service.py` —— 订单提交与优惠计算（当前缺陷：coupon 为空时未做防护）
- `coupon_client.py` —— 下游优惠券服务客户端
- `order_repository.py` —— 存储层（内存实现）
- `tests/` —— 单测

修复 Agent 在此代码库上生成最小化 diff，并经「应用 + 编译」双重校验后输出建议供人工评审。
