"""文件交付接口：导出清单、下载结果表、打包下载；三种产物都从批次冻结快照生成。"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services.delivery import DeliveryError, DeliveryService
from app.services.cleaning import CleaningBatchService
from app.store import store

router = APIRouter(prefix="/api/delivery", tags=["文件交付"])

service = DeliveryService()
batches = CleaningBatchService()


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    # 中文文件名按 RFC 5987 编码，同时保留 ASCII 回退名。
    disposition = f"attachment; filename=\"delivery\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "X-Batch-Filename": quote(filename),
        },
    )


@router.get("/batches/{batch_id}/export")
def export_batch(batch_id: int) -> Response:
    """导出批次清单 JSON：解析模板、目标字段、去噪规则、两侧结果全部随版本冻结。"""
    try:
        content, filename, _delivery = service.export_batch(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _attachment(content, filename, "application/json")


@router.get("/batches/{batch_id}/download")
def download_results(batch_id: int) -> Response:
    """下载结构化结果 CSV：解析字段结果与清洗结果合表，逐行核对。"""
    try:
        content, filename, _delivery = service.download_table(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _attachment(content, filename, "text/csv")


@router.get("/batches/{batch_id}/package")
def package_batch(batch_id: int) -> Response:
    """打包下载 zip：manifest、解析字段结果、清洗结果、版本对照表、冲突裁决、校验和。"""
    try:
        content, filename, _delivery = service.package_batch(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _attachment(content, filename, "application/zip")


@router.get("/batches/{batch_id}/manifest")
def manifest(batch_id: int) -> dict[str, Any]:
    """在页面上直接查看交付清单，不用先下载文件。"""
    try:
        batch = service._require_batch(batch_id)
    except DeliveryError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return service._manifest(batch)


@router.get("/deliveries")
def list_deliveries() -> dict[str, Any]:
    """交付记录：每条都带批次编号、模板版本与产物指纹，便于追溯文件来源。"""
    rows = sorted(store.rows("_file_deliveries"), key=lambda row: int(row.get("id", 0)), reverse=True)
    return {"total": len(rows), "items": rows}
