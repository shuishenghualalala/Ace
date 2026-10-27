"""Crew 发行版版本号。

顶层 ``crew`` 是 PEP 420 命名空间包（ADR-0037 uv workspace，多发行版共享
``crew.*`` import 名），不允许带 ``__init__.py``，版本常量改由本模块承载。
"""

__version__ = "0.1.0"
