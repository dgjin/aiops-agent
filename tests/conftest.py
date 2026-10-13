"""pytest 全局配置：强制 demo 模式（测试永不连接生产数据库）。

``AIOPS_MODE`` 生产默认是 ``production``（fail-fast 需要），但测试必须确定性走
demo 文件后端；此处**强制覆盖**（而非 setdefault），防止开发者 shell 中导出的
``AIOPS_MODE=production`` 把测试指到真实数据库。DB 层单测自行用
``mock.patch.dict`` 临时切换到 SQLite 临时库（见 test_db.py）。
"""

import os

os.environ["AIOPS_MODE"] = "demo"
# 置空而非移除：pytest 下 tests/__init__.py 先于本文件执行并置 ""（test_production_safety
# 断言空串），pop 会破坏该契约；空串与缺失同样阻断对真实库的连接，语义等价。
os.environ["AIOPS_DATABASE_URL"] = ""
