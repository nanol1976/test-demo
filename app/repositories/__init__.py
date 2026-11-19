"""台账仓储层。

铁律：本层只允许「新增记录」与「更新状态字段」，
禁止任何删除、清空、批量覆盖能力（delete / truncate / bulk_update / upsert-覆盖）。
"""
from app.repositories.ledger import LedgerRepository

__all__ = ["LedgerRepository"]
