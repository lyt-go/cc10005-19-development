"""清洗批次引擎：把一个解析模板在某一次提交时的全部口径冻结下来，并逐字段执行。

设计要求（对应验收口径）：
- 提交解析模板时冻结目标字段、XPath、类型转换规则、去噪规则版本以及各组件版本；
- 生成可核对的「解析字段结果」和「清洗结果」，两者逐行逐字段一一对应；
- 规则改版后形成新批次，旧批次原样保留（批次里存的是冻结快照，不引用可变记录）；
- 同一字段命中多条去噪规则时有确定性的冲突裁决，并记录裁决理由；
- 重复提交（同一模板、同一冻结口径）不生成重复批次；
- 解析与清洗写入失败时整批回滚，不会留下只更新一侧的字段类型。

默认填充值与敏感词脱敏的优先级：
先执行全部去噪步骤（含敏感词脱敏），再应用默认填充值。这样填充值不会在
脱敏之后被重新写入未脱敏的内容；填充值来源优先取目标字段上的配置，字段未配置时
回退到生效去噪规则上的默认填充值，并在清洗结果里记录来源和版本。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.services import versions
from app.services.xpath import XPathError, parse_fragment, select, select_text, validate
from app.store import store

PARSER_MODULE = "parser"
FIELD_MODULE = "parser_target_fields"
CLEAN_MODULE = "dataclean"
BATCH_MODULE = "_cleaning_batches"
FIELD_RESULT_MODULE = "_batch_field_results"
CLEAN_RESULT_MODULE = "_batch_clean_results"
AUDIT_MODULE = "audittrail"

FROZEN_RULE_COMPONENTS = [
    "HTML标签清理", "特殊字符过滤", "空白规整模式", "长度截断阈值",
    "编码纠错", "敏感词脱敏", "正则匹配式", "正则用途",
    "脱敏替换模板", "敏感词表", "默认填充值", "优先级",
    "HTML标签清理动作", "特殊字符过滤动作", "空白规整动作",
    "编码纠错动作", "敏感词脱敏动作",
]
MULTI_TYPES = ("文本列表", "列表", "多值文本")
HIT_SENTINEL = "\u0000__XPATH_MISS__"
CONVERSION_TYPES = ("文本列表", "列表", "多值文本", "数值区间")


class BatchError(ValueError):
    """批次生成被业务规则拒绝，message 直接返回给调用方。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def split_targets(value: Any) -> list[str]:
    return [item.strip() for item in re.split(r"[|,，、;；\s]+", str(value or "")) if item.strip()]


def normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """只保留参与执行的字段，保证指纹不受无关展示字段影响。"""
    frozen = {field: rule.get(field) for field in FROZEN_RULE_COMPONENTS}
    for key in ("规则编号", "规则版本", "正则版本", "默认值版本", "脱敏版本", "规则优先级版本"):
        frozen[key] = rule.get(key, "")
    return frozen


def resolve_conflicts(
    target: str, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """对同一字段命中的多条去噪规则做确定性裁决。

    生效顺序：
    1. 规则优先级数值大者优先；
    2. 同优先级时「脱敏」用途优先（避免被普通过滤规则顶掉）；
    3. 同优先级同用途时规则版本序号大者优先；
    4. 仍然相同则按规则编号升序，保证结果可复现。
    """
    if not candidates:
        return None, []
    ordered = sorted(
        candidates,
        key=lambda row: (
            -int(row.get("优先级") or 0),
            0 if str(row.get("正则用途") or "") == "脱敏" else 1,
            -versions.rank(row.get("规则版本")),
            str(row.get("规则编号") or ""),
        ),
    )
    winner = ordered[0]
    notes: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        if index == 0:
            reason = "优先级最高，同优先级下脱敏优先、版本最新，作为唯一生效规则"
        elif int(row.get("优先级") or 0) != int(winner.get("优先级") or 0):
            reason = f"优先级 {row.get('优先级')} 低于生效规则的 {winner.get('优先级')}"
        elif str(row.get("正则用途") or "") != "脱敏":
            reason = "同优先级时脱敏规则优先，本条未生效"
        elif versions.rank(row.get("规则版本")) != versions.rank(winner.get("规则版本")):
            reason = "同优先级同用途时版本较旧，本条未生效"
        else:
            reason = "全部排序键相同，按规则编号升序取第一条"
        notes.append({
            "字段": target,
            "规则编号": row.get("规则编号"),
            "规则版本": row.get("规则版本"),
            "优先级": row.get("优先级"),
            "正则用途": row.get("正则用途"),
            "是否生效": index == 0,
            "裁决理由": reason,
        })
    return winner, notes


def _step(trace: dict[str, Any], name: str, version: str, before: list[str], after: list[str]) -> None:
    trace["步骤"].append({"步骤": name, "版本": version, "输入": list(before), "输出": list(after)})


def apply_rule(tokens: list[str], field_type: str, rule: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """按冻结的规则版本清洗多值列表；分隔符拆分已在上游完成，这里逐项清洗。"""
    multi = field_type in MULTI_TYPES
    trace: dict[str, Any] = {"规则编号": rule["规则编号"], "规则版本": rule["规则版本"], "步骤": []}
    values = list(tokens)
    if not values:
        return values, trace

    if str(rule.get("HTML标签清理动作") or "none") == "strip_tags":
        cleaned = [re.sub(r"<[^>]+>", "", item) for item in values]
        _step(trace, "HTML标签清理", rule["规则版本"], values, cleaned)
        values = cleaned

    pattern = str(rule.get("正则匹配式") or "")
    usage = str(rule.get("正则用途") or "过滤")
    if usage == "过滤" and pattern and str(rule.get("特殊字符过滤动作") or "none") == "regex_filter":
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise BatchError(f"去噪规则 {rule['规则编号']} 的正则表达式无法编译：{error}") from error
        cleaned = [compiled.sub("", item) for item in values]
        _step(trace, "特殊字符过滤", rule.get("正则版本") or rule["规则版本"], values, cleaned)
        values = cleaned

    action = str(rule.get("空白规整动作") or "trim")
    if action == "collapse":
        cleaned = [re.sub(r"[\s\u0000-\u001f]+", " ", item).strip() for item in values]
    else:
        cleaned = [item.strip() for item in values]
    _step(trace, "空白规整", rule["规则版本"], values, cleaned)
    values = cleaned

    limit = int(rule.get("长度截断阈值") or 0)
    if limit > 0:
        cleaned = [item[:limit] for item in values]
        if cleaned != values:
            _step(trace, "长度截断", rule["规则版本"], values, cleaned)
        values = cleaned

    if str(rule.get("编码纠错动作") or "none") == "utf8_redecode":
        cleaned = [item.replace("�", "") for item in values]
        if cleaned != values:
            _step(trace, "编码纠错", rule["规则版本"], values, cleaned)
        values = cleaned

    if str(rule.get("敏感词脱敏动作") or "none") == "mask_terms":
        cleaned = list(values)
        if pattern and usage == "脱敏":
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise BatchError(f"去噪规则 {rule['规则编号']} 的脱敏正则无法编译：{error}") from error
            replacement = str(rule.get("脱敏替换模板") or "")
            cleaned = [compiled.sub(replacement, item) for item in cleaned]
        terms = [term for term in re.split(r"[|,，、]\s*", str(rule.get("敏感词表") or "")) if term]
        for term in terms:
            cleaned = [item.replace(term, "*" * len(term)) for item in cleaned]
        if cleaned != values:
            _step(trace, "敏感词脱敏", rule.get("脱敏版本") or rule["规则版本"], values, cleaned)
        values = cleaned

    if multi:
        values = [item for item in values if item != ""]
    else:
        # 单值字段清洗后只剩空白，同样视为空值，交给默认填充值/空值原因处理。
        values = [item for item in values if item != ""]
    return values, trace


def convert_value(text: str, field_type: str) -> Any:
    """类型转换：把文本转成目标字段声明的类型；无法转换时返回 None，由调用方解释。"""
    token = text.strip()
    if field_type == "整数":
        digits = re.sub(r"[^0-9-]", "", token)
        if digits in ("", "-"):
            return None
        try:
            return int(digits)
        except ValueError:
            return None
    if field_type == "数值":
        match = re.search(r"-?\d+(?:\.\d+)?", token.replace(",", ""))
        return round(float(match.group(0)), 2) if match else None
    if field_type == "数值区间":
        numbers = re.findall(r"\d+(?:\.\d+)?", token.lower().replace("k", "000"))
        if not numbers:
            return None
        low = float(numbers[0])
        high = float(numbers[1]) if len(numbers) > 1 else low
        return [low, high]
    if field_type in MULTI_TYPES:
        return token
    return token


def parse_field_value(text: str, field: dict[str, Any]) -> tuple[Any, str]:
    """先按字段自己的正则取子串，再做类型转换；返回 (解析结果, 类型转换说明)。"""
    raw = text.strip()
    pattern = str(field.get("正则匹配式") or "").strip()
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise BatchError(f"字段「{field['字段名']}」的正则匹配式无法编译：{error}") from error
        match = compiled.search(text)
        if match:
            raw = (match.group(1) if match.groups() else match.group(0)).strip()
    field_type = str(field.get("字段类型") or "文本")
    converted = convert_value(raw, field_type)
    if converted is None:
        return None, f"按「{field.get('类型转换规则') or field_type}」转换失败，解析值为空"
    return converted, f"按「{field.get('类型转换规则') or field_type}」转换为{field_type}"


def render_tokens(parsed: Any, field: dict[str, Any]) -> tuple[list[str], str]:
    """把解析结果展开成待清洗的多值列表；返回 (值序列, 实际分隔符)。

    多值字段按声明分隔符拆成逐项序列；区间等结构化值渲染成「下界-上界」，
    避免把 Python 列表直接拼成不可读的字符串。
    """
    separator = str(field.get("多值分隔符") or "")
    field_type = str(field.get("字段类型") or "文本")
    if isinstance(parsed, (list, tuple)):
        if field_type in MULTI_TYPES and separator:
            return [str(item).strip() for item in parsed], separator
        return ["-".join(str(item) for item in parsed)], ""
    if field_type in MULTI_TYPES:
        if not separator:
            return [str(parsed)], ""
        return [item.strip() for item in str(parsed).split(separator)], separator
    return [str(parsed)], ""


def _empty_reason(
    *,
    matched: bool,
    hit: bool,
    text: str,
    parsed: Any,
    cleaned: list[str],
    separator: str,
    has_default: bool,
) -> str:
    """解释字段为什么是空值，回看批次时不用再猜。"""
    if not matched:
        return "XPath 未命中该记录，解析值为空"
    if not hit:
        return "XPath 命中节点但文本为空"
    if parsed is None:
        return "类型转换失败，解析值为空"
    if not cleaned and separator:
        return "按分隔符拆分并去噪后无有效多值项" if not has_default else ""
    if not cleaned:
        return "去噪后为空且未配置默认填充值" if not has_default else ""
    return ""


def fingerprint_of(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class CleaningBatchService:
    # ------------------------------------------------------------------ 查询
    def list_batches(self, *, template_no: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        result = rows
        if template_no:
            result = [row for row in result if row.get("模板编号") == template_no]
        if status:
            result = [row for row in result if row.get("status") == status]
        return sorted(result, key=lambda row: int(row.get("id", 0)), reverse=True)

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def field_results(self, batch_id: int) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row) for row in store.rows(FIELD_RESULT_MODULE)
            if row.get("批次编号") == batch_id
        ]

    def clean_results(self, batch_id: int) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row) for row in store.rows(CLEAN_RESULT_MODULE)
            if row.get("批次编号") == batch_id
        ]

    def count_field_rows(self, batch_id: int) -> int:
        return sum(1 for row in store.rows(FIELD_RESULT_MODULE) if row.get("批次编号") == batch_id)

    def count_clean_rows(self, batch_id: int) -> int:
        return sum(1 for row in store.rows(CLEAN_RESULT_MODULE) if row.get("批次编号") == batch_id)

    # ------------------------------------------------------------------ 生成
    def submit_template(self, template_entry_id: int) -> dict[str, Any]:
        """提交解析模板并冻结口径生成清洗批次；重复提交返回已有批次。"""
        template = store.find(PARSER_MODULE, template_entry_id)
        if template is None:
            raise BatchError(f"解析模板 {template_entry_id} 不存在或已归档")
        if template.get("status") == "已废弃":
            raise BatchError(f"解析模板 {template['模板编号']} 已废弃，不能提交生成清洗批次")

        fields = [
            copy.deepcopy(row) for row in store.rows(FIELD_MODULE)
            if row.get("模板编号") == template["模板编号"] and row.get("status") != "已废弃"
        ]
        if not fields:
            raise BatchError(f"解析模板 {template['模板编号']} 未登记目标字段，无法生成清洗批次")
        expected = int(template.get("目标字段数") or 0)
        if expected and len(fields) != expected:
            raise BatchError(
                f"解析模板 {template['模板编号']} 声明 {expected} 个目标字段，"
                f"实际登记 {len(fields)} 个，字段类型不一致，已拒绝生成批次"
            )
        for field in fields:
            try:
                validate(str(field.get("XPath表达式") or ""))
            except XPathError as error:
                raise BatchError(f"字段「{field['字段名']}」的 XPath 不可用：{error}") from error

        try:
            document = parse_fragment(str(template.get("采集示例") or ""))
        except XPathError as error:
            raise BatchError(str(error)) from error

        rules_by_field: dict[str, dict[str, Any]] = {}
        conflicts: list[dict[str, Any]] = []
        for field in fields:
            candidates = [
                copy.deepcopy(row) for row in store.rows(CLEAN_MODULE)
                if row.get("status") == "已启用" and field["字段名"] in split_targets(row.get("目标字段"))
            ]
            winner, notes = resolve_conflicts(field["字段名"], candidates)
            conflicts.extend(notes)
            if winner is not None:
                rules_by_field[field["字段名"]] = winner

        frozen_fields: list[dict[str, Any]] = []
        for field in fields:
            winner = rules_by_field.get(field["字段名"])
            frozen_fields.append({
                "字段名": field["字段名"],
                "字段版本": field.get("字段版本", ""),
                "字段类型": field.get("字段类型", "文本"),
                "XPath表达式": field.get("XPath表达式", ""),
                "XPath版本": field.get("XPath版本", ""),
                "正则匹配式": field.get("正则匹配式", ""),
                "正则版本": field.get("正则版本", ""),
                "类型转换规则": field.get("类型转换规则", ""),
                "类型转换版本": field.get("类型转换版本", ""),
                "默认填充值": field.get("默认填充值", ""),
                "默认值版本": field.get("默认值版本", ""),
                "多值分隔符": field.get("多值分隔符", ""),
                "分隔符版本": field.get("分隔符版本", ""),
                "去噪规则编号": winner["规则编号"] if winner else "",
                "去噪规则版本": winner["规则版本"] if winner else "",
                "去噪规则": normalize_rule(winner) if winner else None,
            })

        frozen_rules = {
            field["去噪规则编号"]: normalize_rule(rules_by_field[field["字段名"]])
            for field in frozen_fields
            if field["去噪规则编号"]
        }

        frozen_template = {
            "模板编号": template["模板编号"],
            "模板版本": template.get("模板版本", ""),
            "XPath表达式": template.get("XPath表达式", ""),
            "XPath版本": template.get("XPath版本", ""),
            "正则匹配式": template.get("正则匹配式", ""),
            "正则版本": template.get("正则版本", ""),
            "类型转换规则": template.get("类型转换规则", ""),
            "类型转换版本": template.get("类型转换版本", ""),
            "默认填充值": template.get("默认填充值", ""),
            "默认值版本": template.get("默认值版本", ""),
            "多值分隔符": template.get("多值分隔符", ""),
            "分隔符版本": template.get("分隔符版本", ""),
            "目标字段数": expected,
            "嵌套结构深度": template.get("嵌套结构深度", 0),
        }

        fingerprint_payload = {
            "解析模板": frozen_template,
            "目标字段": frozen_fields,
            "去噪规则": frozen_rules,
            "冲突裁决": [
                {key: note[key] for key in ("字段", "规则编号", "规则版本", "是否生效", "裁决理由")}
                for note in conflicts
            ],
        }
        fingerprint = fingerprint_of(fingerprint_payload)

        anomalies = [
            {
                "字段": field["字段名"],
                "说明": (
                    f"字段声明类型「{field.get('字段类型')}」与类型转换规则不一致"
                    if field.get("字段类型") in CONVERSION_TYPES
                    and field["字段类型"] not in str(field.get("类型转换规则") or field["字段类型"])
                    else ""
                ),
            }
            for field in fields
        ]
        anomalies = [item for item in anomalies if item["说明"]]
        if expected and len(fields) != expected:
            anomalies.append({"字段": "—", "说明": f"声明 {expected} 个目标字段，实际登记 {len(fields)} 个"})

        existing = next((row for row in store.rows(BATCH_MODULE) if row.get("指纹") == fingerprint), None)
        if existing is not None:
            return {
                "ok": True,
                "message": f"同一冻结口径已存在清洗批次 {existing['批次编号']}，未重复生成",
                "batch": copy.deepcopy(existing),
                "duplicated": True,
            }

        existing_for_template = [row for row in store.rows(BATCH_MODULE) if row.get("模板编号") == template["模板编号"]]
        batch_no = f"{template['模板编号']}-CLEAN-{len(existing_for_template) + 1:04d}"

        # ---- 先算出两侧结果，全部成功后才一次性落库 ----
        field_rows: list[dict[str, Any]] = []
        clean_rows: list[dict[str, Any]] = []
        try:
            # 模板 XPath 选中的是「记录容器」，字段 XPath 以 . 开头时逐容器求值；
            # 绝对路径则全文档求值后按记录序号对齐。两侧行键始终是 (字段名, 记录序号)。
            record_nodes = select(document, template["XPath表达式"]) if str(template.get("XPath表达式") or "").strip() else []
            record_count = max(len(record_nodes), 1)
            if settings.cleaning_failure_injection == "empty_xpath":
                # 故障注入：模拟 XPath 适配失败，验证不会留下半套结果。
                raise BatchError("故障注入：解析阶段 XPath 求值失败，批次未写入任何结果")

            def field_texts(field: dict[str, Any]) -> list[str]:
                expression = str(field["XPath表达式"])
                if expression.startswith(".") and record_nodes:
                    texts: list[str] = []
                    for node in record_nodes:
                        found = select_text(node, expression)
                        # 记录内没有该节点时放一个哨兵，区分「未命中」与「命中但为空」。
                        texts.append(found[0] if found else HIT_SENTINEL)
                    return texts
                return select_text(document, expression)

            hits_by_field = {field["字段名"]: field_texts(field) for field in frozen_fields}

            for field in frozen_fields:
                texts = hits_by_field[field["字段名"]]
                for record_index in range(record_count):
                    # matched 表示 XPath 是否选中节点；hit 表示选中的节点是否有非空文本。
                    matched = HIT_SENTINEL not in texts and record_index < len(texts)
                    hit = matched and bool(texts[record_index].strip())
                    text = texts[record_index] if matched else ""
                    parsed, conversion_note = parse_field_value(text, field) if hit else (
                        None,
                        "命中文本为空" if matched else "XPath 未命中该记录",
                    )
                    tokens, separator = render_tokens(parsed, field) if parsed is not None else ([], str(field.get("多值分隔符") or ""))

                    rule = field["去噪规则"]
                    if rule is not None and tokens:
                        clean_values, trace = apply_rule(tokens, field["字段类型"], rule)
                    else:
                        clean_values, trace = list(tokens), {"规则编号": "", "规则版本": "", "步骤": []}
                        clean_values = [item for item in clean_values if item != ""]

                    field_default = str(field.get("默认填充值") or "")
                    rule_default = str((rule or {}).get("默认填充值") or "")
                    effective_default = field_default or rule_default
                    default_source = "目标字段" if field_default else ("去噪规则" if rule_default else "")
                    default_version = (
                        field.get("默认值版本", "") if field_default
                        else (rule or {}).get("默认值版本", "")
                    )
                    # 先记录「清洗后为空」的真实原因，再应用默认填充值；
                    # 这样回看时既能知道为什么是空值，也能知道是谁把值补上的。
                    empty_reason = _empty_reason(
                        matched=matched,
                        hit=hit,
                        text=text,
                        parsed=parsed,
                        cleaned=clean_values,
                        separator=separator,
                        has_default=False,
                    )
                    filled = False
                    if not clean_values and effective_default:
                        if separator:
                            clean_values = [
                                item.strip() for item in effective_default.split(separator) if item.strip()
                            ]
                        if not clean_values:
                            clean_values = [effective_default]
                        filled = bool(clean_values)
                        trace["默认填充值"] = {
                            "来源": default_source,
                            "版本": default_version,
                            "值": effective_default,
                            "原因": "清洗后为空，按默认填充值补齐（默认填充值在脱敏之后应用）",
                        }
                    if rule is not None:
                        trace["敏感词脱敏"] = {
                            "版本": rule.get("脱敏版本") or rule["规则版本"],
                            "启用": str(rule.get("敏感词脱敏动作") or "none") != "none",
                            "字段生效": any(
                                step["步骤"] == "敏感词脱敏" for step in trace.get("步骤", [])
                            ),
                        }

                    field_rows.append({
                        "id": len(field_rows) + 1,
                        "批次编号": 0,
                        "字段名": field["字段名"],
                        "字段版本": field["字段版本"],
                        "字段类型": field["字段类型"],
                        "记录序号": record_index + 1,
                        "XPath表达式": field["XPath表达式"],
                        "XPath版本": field["XPath版本"],
                        "XPath命中": hit,
                        "记录容器表达式": template["XPath表达式"],
                        "记录容器XPath版本": frozen_template["XPath版本"],
                        "类型转换规则": field["类型转换规则"],
                        "类型转换版本": field["类型转换版本"],
                        "命中文本": text,
                        "解析值": parsed,
                        "解析值序列": list(tokens),
                        "类型转换说明": conversion_note,
                        "默认填充值": effective_default,
                        "默认值来源": default_source,
                        "默认值版本": default_version,
                        "多值分隔符": separator,
                        "分隔符版本": field["分隔符版本"],
                    })
                    clean_rows.append({
                        "id": len(clean_rows) + 1,
                        "批次编号": 0,
                        "字段名": field["字段名"],
                        "字段类型": field["字段类型"],
                        "记录序号": record_index + 1,
                        "多值分隔符": separator,
                        "原始值": parsed,
                        "原始值序列": list(tokens),
                        "清洗值": clean_values,
                        "清洗值序列": list(clean_values),
                        "多值项数": len(clean_values) if separator else (0 if not clean_values else 1),
                        "去噪规则编号": field["去噪规则编号"],
                        "去噪规则版本": field["去噪规则版本"],
                        "执行轨迹": trace,
                        "使用默认填充值": filled,
                        "空值原因": empty_reason,
                    })
        except BatchError:
            raise
        except Exception as error:
            raise BatchError(f"解析/清洗执行失败：{error}") from error

        try:
            with store.atomic(BATCH_MODULE, FIELD_RESULT_MODULE, CLEAN_RESULT_MODULE, AUDIT_MODULE):
                batch_id = max((int(row.get("id", 0)) for row in store.rows(BATCH_MODULE)), default=0) + 1
                for row in field_rows:
                    row["批次编号"] = batch_id
                for row in clean_rows:
                    row["批次编号"] = batch_id
                batch = {
                    "id": batch_id,
                    "批次编号": batch_no,
                    "模板编号": template["模板编号"],
                    "模板版本": frozen_template["模板版本"],
                    "解析模板": copy.deepcopy(frozen_template),
                    "目标字段": copy.deepcopy(frozen_fields),
                    "去噪规则": copy.deepcopy(frozen_rules),
                    "冲突裁决": copy.deepcopy(conflicts),
                    "冻结时间": _now(),
                    "提交人": "值班管理员",
                    "指纹": fingerprint,
                    "status": "已生成",
                    "pending": False,
                    "abnormal": bool(anomalies),
                    "异常明细": anomalies,
                    "冲突裁决数": len(conflicts),
                    "字段结果数": len(field_rows),
                    "清洗结果数": len(clean_rows),
                    "默认值与脱敏优先级": (
                        "先执行去噪规则（含敏感词脱敏），再应用默认填充值；"
                        "填充值来源优先目标字段，其次生效去噪规则；填充值不会再被脱敏改写"
                    ),
                }
                store.rows(BATCH_MODULE).append(batch)
                store.rows(FIELD_RESULT_MODULE).extend(field_rows)
                if settings.cleaning_failure_injection == "fail_after":
                    # 故障注入：解析结果已写、清洗结果写入失败，必须整批回滚。
                    raise RuntimeError("故障注入：清洗结果写入失败")
                store.rows(CLEAN_RESULT_MODULE).extend(clean_rows)
                store.rows(AUDIT_MODULE).append({
                    "id": max((int(row.get("id", 0)) for row in store.rows(AUDIT_MODULE)), default=0) + 1,
                    "status": "已记录",
                    "pending": False,
                    "abnormal": False,
                    "记录编号": f"BATCH-{batch_id:04d}",
                    "操作类型": "生成清洗批次",
                    "操作对象": batch_no,
                    "操作人": "值班管理员",
                    "操作时间": batch["冻结时间"],
                    "操作IP": "127.0.0.1",
                    "变更详情": (
                        f"冻结模板 {template['模板编号']} 版本 {frozen_template['模板版本']}，"
                        f"字段 {len(frozen_fields)} 个，解析结果 {len(field_rows)} 行"
                    ),
                    "操作结果": "成功",
                })
        except Exception as error:
            raise BatchError(f"批次写入失败，已整批回滚，未留下单侧字段类型：{error}") from error
        return {"ok": True, "message": f"清洗批次 {batch_no} 已生成", "batch": batch, "duplicated": False}
