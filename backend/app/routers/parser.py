"""字段解析器接口：维护解析模板，覆盖提交模板验证、生效模板、废弃模板等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cleaning import BatchError, CleaningBatchService
from app.services.parser import ParserService

router = APIRouter(prefix="/api/parser", tags=["字段解析器"])

service = ParserService()
batches = CleaningBatchService()

LIST_FIELDS = ["模板编号", "目标字段数", "XPath表达式", "正则匹配式", "类型转换规则", "默认填充值", "多值分隔符", "嵌套结构深度"]
STATUSES = ["草稿", "模板验证中", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、模板验证中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤字段解析器列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出字段解析器清单：每个模板带模板版本和各组件版本，附带目标字段口径。"""
    items, total = service.list_entries(page=1, size=10000)
    enriched = []
    for entry in items:
        enriched.append({
            **entry,
            "目标字段": service.list_fields(str(entry["模板编号"])),
        })
    return {
        "module": "parser",
        "total": total,
        "exported_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "version_scheme": "<作用域>.<组件>@V<n>",
        "items": enriched,
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/fields", response_model=dict)
def list_fields(entry_id: int) -> dict:
    """读取模板下的目标字段（含字段类型与各组件版本），批次冻结的就是这份口径。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    fields = service.list_fields(str(entry["模板编号"]))
    return {"模板编号": entry["模板编号"], "模板版本": entry.get("模板版本"), "total": len(fields), "items": fields}


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改模板或目标字段：只递增变化组件的版本，模板版本随之递增，旧批次不受影响。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析模板执行提交模板验证、生效模板、废弃模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/submit", response_model=ActionResult)
def submit_template(entry_id: int) -> ActionResult:
    """提交解析模板并冻结口径生成清洗批次；重复提交同一口径不会生成重复批次。"""
    try:
        outcome = batches.submit_template(entry_id)
    except BatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=outcome["ok"], message=outcome["message"], entry=outcome["batch"])
