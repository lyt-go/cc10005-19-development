"""文件交付链路：导出、下载、打包都保留版本信息。

所有产物都从「清洗批次的冻结快照」生成，不重新读取当前规则，因此：
- 文件里的解析模板、去噪规则、目标字段与批次结果一致；
- 规则改版后再导出，旧批次导出物仍保持旧版本口径；
- 导出物带批次编号、模板版本、规则版本、字段版本和结果指纹，可逐字段核对。
"""
from __future__ import annotations

import copy
import csv
import io
import json
import zipfile
from typing import Any

from app.services.cleaning import CleaningBatchService, split_targets
from app.store import store

DELIVERY_MODULE = "_file_deliveries"


class DeliveryError(ValueError):
    """交付文件无法生成。"""


class DeliveryService:
    def __init__(self) -> None:
        self.batches = CleaningBatchService()

    # ------------------------------------------------------------------ 内部
    def _require_batch(self, batch_id: int) -> dict[str, Any]:
        batch = self.batches.get_batch(batch_id)
        if batch is None:
            raise DeliveryError(f"清洗批次 {batch_id} 不存在或已归档")
        return batch

    def _fields(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return self.batches.field_results(int(batch["id"]))

    def _cleans(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return self.batches.clean_results(int(batch["id"]))

    def _manifest(self, batch: dict[str, Any]) -> dict[str, Any]:
        """交付物清单：版本信息与结果一一对应，供下载后核对。"""
        fields = self._fields(batch)
        cleans = self._cleans(batch)
        return {
            "批次编号": batch["批次编号"],
            "模板编号": batch["模板编号"],
            "模板版本": batch["模板版本"],
            "冻结时间": batch["冻结时间"],
            "fingerprint": batch["指纹"],
            "解析模板": copy.deepcopy(batch["解析模板"]),
            "目标字段": [
                {
                    "字段名": item["字段名"],
                    "字段版本": item["字段版本"],
                    "字段类型": item["字段类型"],
                    "XPath表达式": item["XPath表达式"],
                    "XPath版本": item["XPath版本"],
                    "正则匹配式": item["正则匹配式"],
                    "正则版本": item["正则版本"],
                    "类型转换规则": item["类型转换规则"],
                    "类型转换版本": item["类型转换版本"],
                    "默认填充值": item["默认填充值"],
                    "默认值版本": item["默认值版本"],
                    "多值分隔符": item["多值分隔符"],
                    "分隔符版本": item["分隔符版本"],
                    "去噪规则编号": item["去噪规则编号"],
                    "去噪规则版本": item["去噪规则版本"],
                }
                for item in batch["目标字段"]
            ],
            "去噪规则": copy.deepcopy(batch["去噪规则"]),
            "冲突裁决": copy.deepcopy(batch["冲突裁决"]),
            "默认值与脱敏优先级": batch["默认值与脱敏优先级"],
            "字段结果行数": len(fields),
            "清洗结果行数": len(cleans),
            "字段结果": fields,
            "清洗结果": cleans,
        }

    def _csv(self, rows: list[dict[str, Any]], columns: list[str]) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: self._scalar(row.get(column)) for column in columns})
        return buffer.getvalue()

    @staticmethod
    def _scalar(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return value

    @staticmethod
    def _json_bytes(payload: Any) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _record(self, batch: dict[str, Any], kind: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        delivery_id = max((int(row.get("id", 0)) for row in store.rows(DELIVERY_MODULE)), default=0) + 1
        delivery = {
            "id": delivery_id,
            "交付编号": f"DELIV-{delivery_id:04d}",
            "批次编号": batch["批次编号"],
            "模板编号": batch["模板编号"],
            "模板版本": batch["模板版本"],
            "交付类型": kind,
            "文件名": filename,
            "内容类型": content_type,
            "字节数": len(content),
            "指纹": batch["指纹"],
            "status": "已导出",
            "pending": False,
            "abnormal": False,
        }
        store.rows(DELIVERY_MODULE).append(delivery)
        return delivery

    # ------------------------------------------------------------------ 导出
    def export_batch(self, batch_id: int) -> tuple[bytes, str, dict[str, Any]]:
        """导出可核对的 JSON：模板、规则、目标字段、两侧结果全部随批次版本冻结。"""
        batch = self._require_batch(batch_id)
        manifest = self._manifest(batch)
        filename = f"{batch['批次编号']}-manifest.json"
        content = self._json_bytes(manifest)
        delivery = self._record(batch, "导出", filename, content, "application/json")
        return content, filename, delivery

    def download_table(self, batch_id: int) -> tuple[bytes, str, dict[str, Any]]:
        """下载结构化结果：解析字段结果与清洗结果同一张表，逐行一一对应。"""
        batch = self._require_batch(batch_id)
        fields = {int(row["id"]): row for row in self._fields(batch)}
        cleans = {int(row["id"]): row for row in self._cleans(batch)}
        rows: list[dict[str, Any]] = []
        for result_id in sorted(set(fields) | set(cleans)):
            field = fields.get(result_id, {})
            clean = cleans.get(result_id, {})
            rows.append({
                "批次编号": batch["批次编号"],
                "模板版本": batch["模板版本"],
                "记录序号": clean.get("记录序号", field.get("记录序号")),
                "字段名": clean.get("字段名", field.get("字段名")),
                "字段版本": field.get("字段版本", ""),
                "字段类型": clean.get("字段类型", field.get("字段类型", "")),
                "XPath版本": field.get("XPath版本", ""),
                "类型转换版本": field.get("类型转换版本", ""),
                "解析值": self._scalar(field.get("解析值")),
                "多值分隔符": clean.get("多值分隔符", field.get("多值分隔符", "")),
                "分隔符版本": field.get("分隔符版本", ""),
                "清洗值": self._scalar(clean.get("清洗值")),
                "清洗值序列": self._scalar(clean.get("清洗值序列")),
                "多值项数": clean.get("多值项数"),
                "去噪规则编号": clean.get("去噪规则编号", ""),
                "去噪规则版本": clean.get("去噪规则版本", ""),
                "使用默认填充值": "是" if clean.get("使用默认填充值") else "否",
                "空值原因": clean.get("空值原因", ""),
            })
        buffer = io.StringIO()
        columns = list(rows[0].keys()) if rows else ["批次编号", "字段名", "记录序号"]
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        # 带 BOM，避免在表格软件里打开中文乱码。
        content = ("﻿" + buffer.getvalue()).encode("utf-8")
        filename = f"{batch['批次编号']}-results.csv"
        delivery = self._record(batch, "下载", filename, content, "text/csv")
        return content, filename, delivery

    def package_batch(self, batch_id: int) -> tuple[bytes, str, dict[str, Any]]:
        """打包：manifest + 两侧结果 + 版本对照表 + 校验和，全部落在同一个 zip 里。"""
        batch = self._require_batch(batch_id)
        manifest = self._manifest(batch)
        fields = self._fields(batch)
        cleans = self._cleans(batch)
        conflict_notes = []
        for note in batch.get("冲突裁决", []):
            conflict_notes.append({
                key: self._scalar(note.get(key))
                for key in ("字段", "规则编号", "规则版本", "优先级", "正则用途", "是否生效", "裁决理由")
            })
        version_rows = [
            {
                "对象": "解析模板",
                "编号": batch["模板编号"],
                "版本": batch["模板版本"],
                "XPath版本": batch["解析模板"].get("XPath版本", ""),
                "正则版本": batch["解析模板"].get("正则版本", ""),
                "默认值版本": batch["解析模板"].get("默认值版本", ""),
                "分隔符版本": batch["解析模板"].get("分隔符版本", ""),
                "脱敏版本": "—",
            },
            *[
                {
                    "对象": "目标字段",
                    "编号": f"{field['字段名']}",
                    "版本": field["字段版本"],
                    "XPath版本": field["XPath版本"],
                    "正则版本": field["正则版本"],
                    "默认值版本": field["默认值版本"],
                    "分隔符版本": field["分隔符版本"],
                    "脱敏版本": "—",
                }
                for field in batch["目标字段"]
            ],
            *[
                {
                    "对象": "去噪规则",
                    "编号": rule["规则编号"],
                    "版本": rule["规则版本"],
                    "XPath版本": "—",
                    "正则版本": rule.get("正则版本", ""),
                    "默认值版本": rule.get("默认值版本", ""),
                    "分隔符版本": "—",
                    "脱敏版本": rule.get("脱敏版本", ""),
                }
                for rule in batch["去噪规则"].values()
            ],
        ]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", self._json_bytes(manifest))
            archive.writestr(
                "field-results.csv",
                self._csv(fields, [
                    "批次编号", "字段名", "记录序号", "字段版本", "字段类型",
                    "XPath表达式", "XPath版本", "类型转换规则", "类型转换版本",
                    "命中文本", "解析值", "解析值序列", "默认填充值", "默认值版本",
                    "多值分隔符", "分隔符版本",
                ]).encode("utf-8-sig"),
            )
            archive.writestr(
                "clean-results.csv",
                self._csv(cleans, [
                    "批次编号", "字段名", "记录序号", "字段类型", "原始值", "原始值序列",
                    "清洗值", "清洗值序列", "多值项数", "多值分隔符",
                    "去噪规则编号", "去噪规则版本", "使用默认填充值", "空值原因",
                ]).encode("utf-8-sig"),
            )
            archive.writestr(
                "versions.csv",
                self._csv(version_rows, ["对象", "编号", "版本", "XPath版本", "正则版本", "默认值版本", "分隔符版本", "脱敏版本"]).encode("utf-8-sig"),
            )
            archive.writestr(
                "conflicts.csv",
                self._csv(conflict_notes, ["字段", "规则编号", "规则版本", "优先级", "正则用途", "是否生效", "裁决理由"]).encode("utf-8-sig"),
            )
            checks = {
                "批次编号": batch["批次编号"],
                "模板版本": batch["模板版本"],
                "指纹": batch["指纹"],
                "field-results.csv": self.batches.count_field_rows(batch_id),
                "clean-results.csv": self.batches.count_clean_rows(batch_id),
                "文件": [],
            }
            for name in archive.namelist():
                checks["文件"].append(name)
            archive.writestr("checksums.json", self._json_bytes(checks))
        content = buffer.getvalue()
        filename = f"{batch['批次编号']}-package.zip"
        delivery = self._record(batch, "打包", filename, content, "application/zip")
        return content, filename, delivery

    # ------------------------------------------------------------------ 核对
    def verify_batch(self, batch_id: int) -> dict[str, Any]:
        """核对批次内部一致性：字段版本、两侧行数、多值项与分隔符语义。"""
        batch = self._require_batch(batch_id)
        fields = self._fields(batch)
        cleans = self._cleans(batch)
        issues: list[dict[str, Any]] = []

        if len(fields) != len(cleans):
            issues.append({"级别": "错误", "说明": f"解析字段结果 {len(fields)} 行与清洗结果 {len(cleans)} 行不一致"})
        field_keys = {(row["字段名"], row["记录序号"]) for row in fields}
        clean_keys = {(row["字段名"], row["记录序号"]) for row in cleans}
        if field_keys != clean_keys:
            issues.append({"级别": "错误", "说明": "解析字段结果与清洗结果的行键不一一对应"})

        for field in batch["目标字段"]:
            current = next(
                (row for row in store.rows("parser_target_fields")
                 if row.get("模板编号") == batch["模板编号"] and row.get("字段名") == field["字段名"]),
                None,
            )
            if current is not None and current.get("字段版本") != field["字段版本"]:
                issues.append({
                    "级别": "提示",
                    "字段": field["字段名"],
                    "说明": (
                        f"批次冻结字段版本 {field['字段版本']}，当前字段版本 {current.get('字段版本')}；"
                        "规则已改版，需要提交新批次才能采用新口径"
                    ),
                })
            for row in cleans:
                if row["字段名"] != field["字段名"] or not row["多值分隔符"]:
                    continue
                if len(row["清洗值序列"]) != row["多值项数"]:
                    issues.append({
                        "级别": "错误",
                        "字段": field["字段名"],
                        "记录": row["记录序号"],
                        "说明": "多值项数与清洗值序列长度不一致",
                    })
                if row["清洗值"] != row["清洗值序列"]:
                    issues.append({
                        "级别": "错误",
                        "字段": field["字段名"],
                        "记录": row["记录序号"],
                        "说明": "多值字段的清洗值与逐项序列不一致",
                    })

        for rule in batch["去噪规则"].values():
            current = next(
                (row for row in store.rows("dataclean") if row.get("规则编号") == rule["规则编号"]),
                None,
            )
            if current is not None and current.get("规则版本") != rule["规则版本"]:
                issues.append({
                    "级别": "提示",
                    "规则": rule["规则编号"],
                    "说明": (
                        f"批次冻结规则版本 {rule['规则版本']}，当前规则版本 {current.get('规则版本')}；"
                        "旧批次保持原样"
                    ),
                })

        return {
            "批次编号": batch["批次编号"],
            "模板版本": batch["模板版本"],
            "指纹": batch["指纹"],
            "字段结果行数": len(fields),
            "清洗结果行数": len(cleans),
            "一致": not any(issue["级别"] == "错误" for issue in issues),
            "问题": issues,
        }
