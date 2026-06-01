"""写操作 API 鉴权（防公网未授权 POST/PUT/DELETE）。"""

from fastapi import Header, HTTPException

from ..config import settings


def require_write_token(x_write_token: str | None = Header(default=None, alias="X-Write-Token")) -> None:
    expected = (settings.api_write_token or "").strip()
    if not expected:
        return
    if not x_write_token or x_write_token.strip() != expected:
        raise HTTPException(status_code=401, detail="未授权：缺少或无效的 X-Write-Token")
