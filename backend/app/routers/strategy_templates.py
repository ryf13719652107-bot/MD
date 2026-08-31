import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import now_beijing
from ..database import get_db
from ..models.strategy_template import StrategyParamTemplate
from ..schemas.strategy_template import (
    StrategyParamTemplateCreate,
    StrategyParamTemplateResponse,
    StrategyParamTemplateUpdate,
)

router = APIRouter(prefix="/api/strategy-templates", tags=["strategy_templates"])
logger = logging.getLogger(__name__)

_IDENTITY_KEYS = frozenset({"account_id", "name"})
_MAX_PAYLOAD_BYTES = 32_000


def _strip_identity(patch: dict) -> dict:
    return {k: v for k, v in (patch or {}).items() if k not in _IDENTITY_KEYS}


def _dump_payload(patch: dict) -> str:
    cleaned = _strip_identity(patch)
    raw = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=400, detail="模板内容过大")
    return raw


def _to_response(row: StrategyParamTemplate) -> StrategyParamTemplateResponse:
    try:
        patch = json.loads(row.payload or "{}")
    except json.JSONDecodeError:
        patch = {}
    if not isinstance(patch, dict):
        patch = {}
    return StrategyParamTemplateResponse(
        id=row.id,
        name=row.name,
        patch=_strip_identity(patch),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[StrategyParamTemplateResponse])
async def list_templates(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(StrategyParamTemplate).order_by(StrategyParamTemplate.id.asc())
        )
    ).scalars().all()
    return [_to_response(r) for r in rows]


@router.post("", response_model=StrategyParamTemplateResponse)
async def create_template(
    data: StrategyParamTemplateCreate,
    db: AsyncSession = Depends(get_db),
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请输入模板名称")
    payload = _dump_payload(data.patch)
    existing = (
        await db.execute(
            select(StrategyParamTemplate).where(StrategyParamTemplate.name == name)
        )
    ).scalar_one_or_none()
    if existing:
        existing.payload = payload
        existing.updated_at = now_beijing()
        await db.commit()
        await db.refresh(existing)
        return _to_response(existing)
    row = StrategyParamTemplate(name=name, payload=payload)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.put("/{template_id}", response_model=StrategyParamTemplateResponse)
async def update_template(
    template_id: int,
    data: StrategyParamTemplateUpdate,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(StrategyParamTemplate, template_id)
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="请输入模板名称")
        clash = (
            await db.execute(
                select(StrategyParamTemplate.id).where(
                    StrategyParamTemplate.name == name,
                    StrategyParamTemplate.id != template_id,
                )
            )
        ).first()
        if clash:
            raise HTTPException(status_code=400, detail="已有同名模板")
        row.name = name
    if data.patch is not None:
        row.payload = _dump_payload(data.patch)
    row.updated_at = now_beijing()
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(StrategyParamTemplate, template_id)
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    await db.delete(row)
    await db.commit()
