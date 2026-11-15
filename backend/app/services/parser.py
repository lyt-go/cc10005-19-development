"""字段解析器业务规则：状态流转、字段校验、版本递增与筛选口径都收在这里。"""
from __future__ import annotations

import re
from typing import Any

from app.services import versions
from app.services.xpath import XPathError, validate as validate_xpath
from app.store import store

MODULE = "parser"
FIELD_MODULE = "parser_target_fields"
REQUIRED_FIELDS = ["模板编号", "目标字段数", "XPath表达式"]
STATUS_ORDER = ["草稿", "模板验证中", "已生效", "已废弃"]
ACTION_RULES = {"提交模板验证": "模板验证中", "生效模板": "已生效", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = []

TEMPLATE_COMPONENTS = [
    ("XPath表达式", "XPath版本", "TEMPLATE.XPATH"),
    ("正则匹配式", "正则版本", "TEMPLATE.REGEX"),
    ("默认填充值", "默认值版本", "TEMPLATE.DEFAULT"),
    ("多值分隔符", "分隔符版本", "TEMPLATE.SEP"),
    ("类型转换规则", "类型转换版本", "TEMPLATE.TYPE"),
]
FIELD_COMPONENTS = [
    ("XPath表达式", "XPath版本", "XPATH"),
    ("正则匹配式", "正则版本", "REGEX"),
    ("默认填充值", "默认值版本", "DEFAULT"),
    ("多值分隔符", "分隔符版本", "SEP"),
    ("类型转换规则", "类型转换版本", "TYPE"),
]


class ParserService:
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_fields(self, template_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(FIELD_MODULE)
            if row.get("模板编号") == template_no and row.get("status") != "已废弃"
        ]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("正则匹配式", "类型转换规则", "默认填充值", "多值分隔符", "嵌套结构深度", "采集示例"):
            entry[field] = values.get(field, "" if field != "嵌套结构深度" else 0)
        template_no = str(entry["模板编号"])
        entry["模板版本"] = versions.initial(template_no, "TEMPLATE")
        for _source, version_field, component in TEMPLATE_COMPONENTS:
            entry[version_field] = versions.initial(template_no, component)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """改模板或改目标字段时，只递增真正变化的组件版本，并让整条规则版本跟着走。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析模板 {entry_id} 不存在或已归档"
        if entry.get("status") == "已废弃":
            return None, f"解析模板 {entry['模板编号']} 已废弃，不能再修改"
        template_no = str(entry["模板编号"])

        # 先整体校验，全部合法后再改内存记录，避免出现「字段已改、XPath 版本没跟」的半套状态。
        if "XPath表达式" in values:
            try:
                validate_xpath(str(values["XPath表达式"]))
            except XPathError as error:
                return None, f"模板 {template_no} 的 XPath 不可用：{error}"
        if values.get("正则匹配式"):
            try:
                re.compile(str(values["正则匹配式"]))
            except re.error as error:
                return None, f"模板 {template_no} 的正则匹配式无法编译：{error}"
        if "fields" in values:
            if not isinstance(values["fields"], list):
                return None, "目标字段变更必须是字段对象数组"
            error = self._validate_field_payload(values["fields"])
            if error:
                return None, error

        changed = False
        changed_components: list[str] = []
        for source, version_field, component in TEMPLATE_COMPONENTS:
            if source not in values:
                continue
            new_value = values[source]
            if str(new_value) != str(entry.get(source, "")):
                entry[source] = new_value
                entry[version_field] = versions.bump(entry.get(version_field), template_no, component)
                changed = True
                changed_components.append(version_field)

        for field in ("目标字段数", "嵌套结构深度", "采集示例"):
            if field in values and str(values[field]) != str(entry.get(field, "")):
                entry[field] = values[field]
                changed = True
                changed_components.append(field)

        if "fields" in values:
            changed_fields, changed_versions = self._apply_field_changes(template_no, values["fields"])
            if changed_fields:
                changed = True
                changed_components.extend(changed_versions)

        if not changed:
            return entry, f"解析模板 {template_no} 没有字段变化，版本保持 {entry.get('模板版本')}"
        entry["模板版本"] = versions.bump(entry.get("模板版本"), template_no, "TEMPLATE")
        entry["pending"] = True
        return entry, (
            f"解析模板 {template_no} 已更新，模板版本升为 {entry['模板版本']}，"
            f"变更组件：{'、'.join(changed_components)}"
        )

    @staticmethod
    def _validate_field_payload(payload: list[dict[str, Any]]) -> str:
        """先整体校验再落库：非法 XPath/正则不能写进模板，避免半套字段定义。"""
        for item in payload:
            if not isinstance(item, dict):
                return "目标字段变更必须是字段对象数组"
            if not str(item.get("字段名") or "").strip():
                return "目标字段缺少字段名"
            if "XPath表达式" in item:
                try:
                    validate_xpath(str(item["XPath表达式"]))
                except XPathError as error:
                    return f"字段「{item['字段名']}」的 XPath 不可用：{error}"
            if item.get("正则匹配式"):
                try:
                    re.compile(str(item["正则匹配式"]))
                except re.error as error:
                    return f"字段「{item['字段名']}」的正则匹配式无法编译：{error}"
        return ""

    def _apply_field_changes(
        self, template_no: str, payload: list[dict[str, Any]]
    ) -> tuple[bool, list[str]]:
        changed = False
        changed_versions: list[str] = []
        for item in payload:
            field_name = str(item.get("字段名") or "").strip()
            existing = next(
                (row for row in store.rows(FIELD_MODULE) if row.get("模板编号") == template_no and row.get("字段名") == field_name),
                None,
            )
            if existing is None:
                scope = f"{template_no}.{field_name}"
                new_row = {
                    "id": max((int(row.get("id", 0)) for row in store.rows(FIELD_MODULE)), default=0) + 1,
                    "status": "模板验证中",
                    "pending": True,
                    "abnormal": False,
                    "模板编号": template_no,
                    "字段名": field_name,
                    "字段类型": item.get("字段类型", "文本"),
                    "XPath表达式": item.get("XPath表达式", ""),
                    "正则匹配式": item.get("正则匹配式", ""),
                    "类型转换规则": item.get("类型转换规则", ""),
                    "默认填充值": item.get("默认填充值", ""),
                    "多值分隔符": item.get("多值分隔符", ""),
                    "字段版本": versions.initial(scope, "FIELD"),
                }
                for _source, version_field, component in FIELD_COMPONENTS:
                    new_row[version_field] = versions.initial(scope, component)
                store.rows(FIELD_MODULE).append(new_row)
                changed = True
                changed_versions.append(f"新增字段:{field_name}")
                continue

            field_changed = False
            scope = f"{template_no}.{field_name}"
            for source, version_field, component in FIELD_COMPONENTS:
                if source not in item:
                    continue
                if str(item[source]) != str(existing.get(source, "")):
                    existing[source] = item[source]
                    existing[version_field] = versions.bump(existing.get(version_field), scope, component)
                    field_changed = True
                    changed_versions.append(f"{field_name}.{version_field}")
            if "字段类型" in item and str(item["字段类型"]) != str(existing.get("字段类型", "")):
                existing["字段类型"] = item["字段类型"]
                field_changed = True
                changed_versions.append(f"{field_name}.字段类型")
            if field_changed:
                existing["字段版本"] = versions.bump(existing.get("字段版本"), scope, "FIELD")
                existing["status"] = "模板验证中"
                existing["pending"] = True
                changed = True
        return changed, changed_versions

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于字段解析器可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        for row in store.rows(FIELD_MODULE):
            if row.get("模板编号") == entry["模板编号"] and row.get("status") != "已废弃":
                row["status"] = target
                row["pending"] = entry["pending"]
        return entry, f"解析模板已{action}"

    def snapshot(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return {"entry": entry, "fields": self.list_fields(str(entry["模板编号"]))}
