"""数据去噪业务规则：状态流转、字段校验、版本递增与筛选口径都收在这里。"""
from __future__ import annotations

import re
from typing import Any

from app.services import versions
from app.store import store

MODULE = "dataclean"
REQUIRED_FIELDS = ["规则编号", "目标字段", "HTML标签清理"]
STATUS_ORDER = ["草稿", "规则验证中", "已启用", "已废弃"]
ACTION_RULES = {"提交规则验证": "规则验证中", "启用规则": "已启用", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

VERSIONED_COMPONENTS = [
    ("正则匹配式", "正则版本", "REGEX"),
    ("默认填充值", "默认值版本", "DEFAULT"),
    ("脱敏替换模板", "脱敏版本", "MASK"),
    ("优先级", "规则优先级版本", "PRIORITY"),
]


class DatacleanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in (
            "规则编号", "特殊字符过滤", "空白规整模式", "长度截断阈值", "编码纠错",
            "敏感词脱敏", "优先级", "正则匹配式", "正则用途", "脱敏替换模板", "敏感词表",
            "默认填充值", "HTML标签清理动作", "特殊字符过滤动作", "空白规整动作",
            "编码纠错动作", "敏感词脱敏动作",
        ):
            if field in values:
                entry[field] = values[field]
        rule_no = str(entry["规则编号"])
        entry.setdefault("优先级", 0)
        entry["规则版本"] = versions.initial(rule_no, "RULE")
        for _source, version_field, component in VERSIONED_COMPONENTS:
            entry[version_field] = versions.initial(rule_no, component)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去噪规则 {entry_id} 不存在或已归档"
        if entry.get("status") == "已废弃":
            return None, f"去噪规则 {entry['规则编号']} 已废弃，不能再修改"
        rule_no = str(entry["规则编号"])

        # 正则、替换模板和数值配置先校验，全部通过后再改记录。
        if values.get("正则匹配式"):
            try:
                re.compile(str(values["正则匹配式"]))
            except re.error as error:
                return None, f"去噪规则 {rule_no} 的正则匹配式无法编译：{error}"
        if "脱敏替换模板" in values:
            template = str(values["脱敏替换模板"])
            try:
                re.sub(r"(\d)(\d{4})(\d{4})", template, "13800138000")
            except re.error as error:
                return None, f"去噪规则 {rule_no} 的脱敏替换模板不可用：{error}"
        if "长度截断阈值" in values:
            try:
                if int(values["长度截断阈值"]) < 0:
                    return None, f"去噪规则 {rule_no} 的长度截断阈值不能为负数"
            except (TypeError, ValueError):
                return None, f"去噪规则 {rule_no} 的长度截断阈值必须是整数"
        if "优先级" in values:
            try:
                int(values["优先级"])
            except (TypeError, ValueError):
                return None, f"去噪规则 {rule_no} 的优先级必须是整数"

        changed = False
        changed_components: list[str] = []
        for source, version_field, component in VERSIONED_COMPONENTS:
            if source in values and str(values[source]) != str(entry.get(source, "")):
                entry[source] = values[source]
                entry[version_field] = versions.bump(entry.get(version_field), rule_no, component)
                changed = True
                changed_components.append(version_field)
        for field in (
            "目标字段", "HTML标签清理", "特殊字符过滤", "空白规整模式", "长度截断阈值",
            "编码纠错", "敏感词脱敏", "正则用途", "敏感词表",
            "HTML标签清理动作", "特殊字符过滤动作", "空白规整动作", "编码纠错动作", "敏感词脱敏动作",
        ):
            if field in values and str(values[field]) != str(entry.get(field, "")):
                entry[field] = values[field]
                changed = True
                changed_components.append(field)
        if not changed:
            return entry, f"去噪规则 {rule_no} 没有字段变化，版本保持 {entry.get('规则版本')}"
        # 规则一旦改版，整条规则版本递增；旧清洗批次仍指向冻结的旧版本。
        entry["规则版本"] = versions.bump(entry.get("规则版本"), rule_no, "RULE")
        entry["pending"] = True
        return entry, (
            f"去噪规则 {rule_no} 已更新，规则版本升为 {entry['规则版本']}，"
            f"变更组件：{'、'.join(changed_components)}"
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去噪规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据去噪可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"去噪规则已{action}"

    def version_summary(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return {
            "规则编号": entry["规则编号"],
            "规则版本": entry.get("规则版本"),
            "正则匹配式": entry.get("正则匹配式"),
            "正则版本": entry.get("正则版本"),
            "默认填充值": entry.get("默认填充值"),
            "默认值版本": entry.get("默认值版本"),
            "脱敏替换模板": entry.get("脱敏替换模板"),
            "脱敏版本": entry.get("脱敏版本"),
            "优先级": entry.get("优先级"),
            "规则优先级版本": entry.get("规则优先级版本"),
        }
