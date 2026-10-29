"""安全管理 API 路由.

端点：
  GET  /security/providers         列出支持的 API Key 提供商
  POST /security/keys/{provider}   保存 API Key
  GET  /security/keys/{provider}   检查 API Key 是否已存储（不返回明文）
  DELETE /security/keys/{provider} 删除 API Key
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from quantbot.common.security.keystore import (
    delete_api_key,
    list_providers,
    load_api_key,
    save_api_key,
)

router = APIRouter(prefix="/security", tags=["安全管理"])


class SaveKeyRequest(BaseModel):
    api_key: str


@router.get("/providers")
def get_providers() -> dict[str, list[str]]:
    """列出支持的 API Key 提供商."""
    return {"providers": list_providers()}


@router.post("/keys/{provider}")
def save_key(provider: str, req: SaveKeyRequest) -> dict[str, str]:
    """保存 API Key 到系统 keyring."""
    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail="API Key 不能为空")
    save_api_key(provider, req.api_key.strip())
    return {"message": f"{provider} API Key 已安全保存"}


@router.get("/keys/{provider}")
def check_key(provider: str) -> dict[str, object]:
    """检查 API Key 是否已存储（不返回明文）."""
    value = load_api_key(provider)
    return {
        "provider": provider,
        "configured": value is not None,
        "preview": f"{value[:4]}****" if value and len(value) > 4 else None,
    }


@router.delete("/keys/{provider}")
def delete_key(provider: str) -> dict[str, str]:
    """删除 API Key."""
    success = delete_api_key(provider)
    if not success:
        raise HTTPException(status_code=404, detail=f"API Key 不存在: {provider}")
    return {"message": f"{provider} API Key 已删除"}
