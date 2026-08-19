"""币安账户持仓推送缓存（ccxt.pro watch_positions），供接针热路径省掉 REST 复核。

断线/过期时自动回落 REST；不替代对账，只做低延迟门禁。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .tick_context import exchange_legs_from_positions

logger = logging.getLogger(__name__)

_RECONNECT_INITIAL = 1.0
_RECONNECT_MAX = 30.0
# 超过此时长无成功更新则视为过期，热路径回落 REST
# 30s：减少 ccxt.pro 心跳稀疏时的无谓 REST 回落；断连降级慢一点但方向安全（旧 legs 只会漏开不会双开）
_STALE_AFTER_SEC = 30.0


def _norm_sym(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").replace("_", "").upper()


class AccountPositionStream:
    """Per account_id：后台 watch_positions → 内存 legs。"""

    def __init__(self) -> None:
        self._legs: dict[int, dict[tuple[str, str], float]] = {}
        self._updated_at: dict[int, float] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._clients: dict[int, object] = {}
        self._owners: dict[int, set[str]] = {}  # account_id → owner keys
        self._lock = asyncio.Lock()

    def get_legs(self, account_id: int) -> dict[tuple[str, str], float] | None:
        """返回腿快照；无缓存则 None。"""
        legs = self._legs.get(account_id)
        if legs is None:
            return None
        return dict(legs)

    def age_sec(self, account_id: int) -> float | None:
        ts = self._updated_at.get(account_id)
        if ts is None:
            return None
        return max(0.0, time.time() - ts)

    def is_fresh(self, account_id: int, *, max_age_sec: float = _STALE_AFTER_SEC) -> bool:
        age = self.age_sec(account_id)
        return age is not None and age <= max_age_sec

    def leg_qty(self, account_id: int, symbol: str, side: str) -> float | None:
        """新鲜缓存上的同向仓数量；过期/无缓存返回 None。"""
        if not self.is_fresh(account_id):
            return None
        legs = self._legs.get(account_id) or {}
        return float(legs.get((_norm_sym(symbol), (side or "").lower()), 0.0) or 0.0)

    def set_leg(
        self, account_id: int, symbol: str, side: str, qty: float
    ) -> None:
        """覆盖写入单腿（REST 复核结果）。"""
        key = (_norm_sym(symbol), (side or "").lower())
        legs = self._legs.setdefault(account_id, {})
        q = float(qty or 0)
        if q > 0:
            legs[key] = q
        else:
            legs.pop(key, None)
        self._updated_at[account_id] = time.time()

    def clear_leg(self, account_id: int, symbol: str, side: str) -> None:
        """平仓后立刻清腿，避免空心跳续期导致假 has_pos 挡新开。"""
        if account_id <= 0:
            return
        self.set_leg(account_id, symbol, side, 0.0)

    def apply_local_fill(
        self, account_id: int, symbol: str, side: str, qty: float
    ) -> None:
        """本地成交后立刻改缓存，避免推送迟到导致重复开。"""
        if qty <= 0:
            return
        key = (_norm_sym(symbol), (side or "").lower())
        legs = self._legs.setdefault(account_id, {})
        legs[key] = float(legs.get(key, 0.0) or 0.0) + float(qty)
        self._updated_at[account_id] = time.time()

    def apply_local_close(
        self, account_id: int, symbol: str, side: str, qty: float | None = None
    ) -> None:
        """本地平仓后立刻减腿；qty 缺省或 ≥ 缓存则清零。"""
        if account_id <= 0:
            return
        key = (_norm_sym(symbol), (side or "").lower())
        legs = self._legs.setdefault(account_id, {})
        cur = float(legs.get(key, 0.0) or 0.0)
        if qty is None or float(qty) >= cur - 1e-12:
            legs.pop(key, None)
        else:
            left = cur - float(qty)
            if left > 1e-12:
                legs[key] = left
            else:
                legs.pop(key, None)
        self._updated_at[account_id] = time.time()

    async def ensure_watching(self, account_id: int, auth_client, *, owner: str) -> None:
        """登记 owner 并确保后台 watch 任务在跑。"""
        async with self._lock:
            owners = self._owners.setdefault(account_id, set())
            owners.add(owner)
            self._clients[account_id] = auth_client
            t = self._tasks.get(account_id)
            if t is None or t.done():
                self._tasks[account_id] = asyncio.create_task(
                    self._run_account(account_id),
                    name=f"acct_pos_ws:{account_id}",
                )

    async def release(self, account_id: int, *, owner: str) -> None:
        async with self._lock:
            owners = self._owners.get(account_id)
            if not owners:
                return
            owners.discard(owner)
            if owners:
                return
            self._owners.pop(account_id, None)
            self._clients.pop(account_id, None)
            t = self._tasks.pop(account_id, None)
            if t is not None and not t.done():
                t.cancel()

    async def seed_from_rest(self, account_id: int, auth_client) -> None:
        """REST 灌一次种子，避免 watch 未到时全空。"""
        try:
            raw = await auth_client.fetch_positions()
            legs = exchange_legs_from_positions(raw or [])
            self._legs[account_id] = dict(legs)
            self._updated_at[account_id] = time.time()
        except Exception as e:
            logger.warning(
                "account_position_stream seed account=%d failed: %s", account_id, e
            )

    def reconcile_from_rest(self, account_id: int, rest_legs: dict[tuple[str, str], float]) -> None:
        """REST 快照对账：清除 UDS 中已不存在的腿（防平仓后 UDS 不清导致假 has_pos）。

        只删不加：UDS 负责低延迟新增，REST 负责清理过期腿。
        """
        uds_legs = self._legs.get(account_id)
        if uds_legs is None:
            return
        stale_keys = [k for k in list(uds_legs) if k not in rest_legs]
        for k in stale_keys:
            uds_legs.pop(k, None)

    def _ingest_positions(self, account_id: int, positions: list) -> None:
        """写入推送。

        空列表只续期、不清仓：ccxt/币安心跳或增量空包若当「全空」会误开双仓。
        非空则整表替换（视为账户持仓快照；已平仓腿不在列表中即清除）。
        """
        if not positions:
            if account_id in self._legs:
                self._updated_at[account_id] = time.time()
            return
        legs = exchange_legs_from_positions(positions)
        self._legs[account_id] = dict(legs)
        self._updated_at[account_id] = time.time()

    async def _run_account(self, account_id: int) -> None:
        backoff = _RECONNECT_INITIAL
        while account_id in self._owners:
            client = self._clients.get(account_id)
            if client is None:
                await asyncio.sleep(0.5)
                continue
            watch = getattr(client, "watch_positions", None)
            if not callable(watch):
                logger.warning(
                    "account_position_stream account=%d: no watch_positions; idle",
                    account_id,
                )
                await asyncio.sleep(30.0)
                continue
            try:
                # 首次 REST 种子
                if account_id not in self._updated_at:
                    await self.seed_from_rest(account_id, client)
                rows = await watch()
                if isinstance(rows, list):
                    self._ingest_positions(account_id, rows)
                elif isinstance(rows, dict):
                    self._ingest_positions(account_id, [rows])
                backoff = _RECONNECT_INITIAL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if account_id not in self._owners:
                    break
                logger.warning(
                    "account_position_stream account=%d ws error: %s; retry in %.1fs",
                    account_id,
                    e,
                    backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, _RECONNECT_MAX)

        self._tasks.pop(account_id, None)


account_position_stream = AccountPositionStream()
