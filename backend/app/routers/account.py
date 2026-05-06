from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.account import Account
from ..schemas.account import AccountCreate, AccountResponse
from ..services.encryption import encrypt, decrypt, mask_key

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.post("", response_model=AccountResponse)
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    try:
        encrypted_key = encrypt(data.api_key)
        encrypted_secret = encrypt(data.api_secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加密失败: {str(e)}")

    account = Account(
        name=data.name,
        api_key_encrypted=encrypted_key,
        api_secret_encrypted=encrypted_secret,
        testnet=data.testnet,
        hedge_mode=data.hedge_mode,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    return AccountResponse(
        id=account.id,
        name=account.name,
        masked_key=mask_key(data.api_key),
        testnet=account.testnet,
        hedge_mode=account.hedge_mode,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account))
    accounts = result.scalars().all()
    resp = []
    for a in accounts:
        try:
            key = decrypt(a.api_key_encrypted)
            mk = mask_key(key)
        except Exception:
            mk = "****"
        resp.append(
            AccountResponse(
                id=a.id,
                name=a.name,
                masked_key=mk,
                testnet=a.testnet,
                hedge_mode=a.hedge_mode,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
        )
    return resp


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    await db.delete(account)
    await db.commit()
