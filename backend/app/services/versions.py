"""版本戳工具：把「模板 / 字段 / 规则 / 组件」的版本统一成可比较、可递增的字符串。

版本格式：``<作用域>.<组件>@V<n>``。改版时只递增真正变化的组件，
并让上一级版本跟着递增，这样回看批次时既能确认整条规则的版本，也能定位到
到底是 XPath、正则还是默认填充值发生了变化。
"""
from __future__ import annotations

import re

_VERSION_RE = re.compile(r"@V(\d+)$")


def initial(scope: str, component: str) -> str:
    return f"{scope}.{component}@V1"


def bump(version: str | None, scope: str, component: str, start: int = 1) -> str:
    """在原有版本上递增；没有版本时按初始版本起算。"""
    if not version:
        return initial(scope, component)
    match = _VERSION_RE.search(str(version))
    if not match:
        return initial(scope, component)
    return f"{scope}.{component}@V{int(match.group(1)) + 1}"


def rank(version: str | None) -> int:
    """取版本里的 V 序号，用于同优先级规则的确定性裁决。"""
    match = _VERSION_RE.search(str(version or ""))
    return int(match.group(1)) if match else 0


def is_newer(left: str | None, right: str | None) -> bool:
    return rank(left) > rank(right)
