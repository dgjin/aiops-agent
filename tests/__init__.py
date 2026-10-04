"""测试包隔离钩子：unittest 体系下强制 demo 模式（pytest 另由 conftest.py 同款覆盖）。

``tests/conftest.py`` 只对 pytest 生效；README 约定的 unittest 运行方式
（``python -m unittest tests.xxx``、``discover -s tests -t .``）会先导入本包
（``tests/__init__.py`` 先于一切测试模块执行），从而在任何 ``aiops_agent``
导入（含 config.py 的 .env 加载，override=False）之前把 ``AIOPS_MODE`` 固定为
demo，杜绝测试进程经 production ``.env`` 直连生产 MySQL。

实测教训（2026-10-04）：``discover -s tests``（不带 ``-t .``）会把测试按顶层
模块导入并跳过本文件——当时 ``.env=production`` 生效，测试直写生产库
（monitored_apps / console_tokens 被测试数据污染，并连带触发 prober 对
http://a.test/ 等测试条目告警）。务必使用带 ``-t .`` 的形式（见 README）。
"""

import os

os.environ["AIOPS_MODE"] = "demo"
os.environ.pop("AIOPS_DATABASE_URL", None)
