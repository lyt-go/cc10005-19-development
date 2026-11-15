"""清洗批次接口：提交解析模板冻结口径、查询批次与两侧结果、核对一致性。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, PageResult
from app.services.cleaning import CleaningBatchService, BatchError
from app.services.delivery import DeliveryError, DeliveryService
from app.store import store

router = APIRouter(prefix="/api/cleaning", tags=["清洗批次"])

batches = CleaningBatchService()
delivery = DeliveryService()


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    template_no: str | None = Query(default=None, description="按模板编号过滤，如 PARS-0002"),
    status: str | None = Query(default=None, description="已生成 / 已冻结"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态列出清洗批次；批次按生成顺序倒序，便于看最新口径。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = batches.list_batches(template_no=template_no, status=status)
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.post("/batches", response_model=ActionResult)
def submit_template(entry_id: int = Query(..., description="解析模板记录 id，如 PARS-0002 的 id")) -> ActionResult:
    """提交解析模板：冻结目标字段、XPath、类型转换规则与去噪规则版本，生成两侧结果。

    重复提交同一冻结口径时返回已有批次，不会产生重复批次。
    """
    try:
        outcome = batches.submit_template(entry_id)
    except BatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=outcome["ok"], message=outcome["message"], entry=outcome["batch"])


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: int) -> dict:
    """读取批次明细，含冻结的模板、目标字段、去噪规则和冲突裁决。"""
    batch = batches.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"清洗批次 {batch_id} 不存在或已归档")
    return batch


@router.get("/batches/{batch_id}/field-results", response_model=dict)
def field_results(batch_id: int) -> dict[str, Any]:
    """读取解析字段结果：每行带字段类型、XPath/类型转换/默认值/分隔符版本。"""
    batch = batches.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"清洗批次 {batch_id} 不存在或已归档")
    rows = batches.field_results(batch_id)
    return {
        "批次编号": batch["批次编号"],
        "模板版本": batch["模板版本"],
        "目标字段": batch["目标字段"],
        "total": len(rows),
        "items": rows,
    }


@router.get("/batches/{batch_id}/clean-results", response_model=dict)
def clean_results(batch_id: int) -> dict[str, Any]:
    """读取清洗结果：多值字段带逐项序列与分隔符版本，空值带可解释原因。"""
    batch = batches.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"清洗批次 {batch_id} 不存在或已归档")
    rows = batches.clean_results(batch_id)
    return {
        "批次编号": batch["批次编号"],
        "模板版本": batch["模板版本"],
        "去噪规则": batch["去噪规则"],
        "total": len(rows),
        "items": rows,
    }


@router.get("/batches/{batch_id}/verify", response_model=dict)
def verify_batch(batch_id: int) -> dict[str, Any]:
    """核对批次：两侧行数/行键一一对应，多值项数与分隔符语义一致，并提示规则是否已改版。"""
    try:
        return delivery.verify_batch(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/batches/{batch_id}/export")
def export_batch(batch_id: int) -> dict[str, Any]:
    """导出带版本信息的批次清单（JSON），字段、规则、结果与批次冻结口径一致。"""
    try:
        delivery.export_batch(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "message": "批次清单已导出", "filename": f"{batches.get_batch(batch_id)['批次编号']}-manifest.json"}


@router.get("", response_model=PageResult[dict])
def index(
    template_no: str | None = Query(default=None, description="按模板编号过滤，如 PARS-0002"),
    status: str | None = Query(default=None, description="已生成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """清洗批次列表：挂在模块根路径下，按模板编号与状态分页。"""
    return list_batches(template_no=template_no, status=status, page=page, size=size)
