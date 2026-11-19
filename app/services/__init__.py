import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from app.models import MessageArchive, Base
from app.schemas import Envelope
from app.core.config import get_settings


class ArchiveService:
    def __init__(self, db: Session, archive_root: Optional[str] = None):
        self.db = db
        self.archive_root = Path(archive_root or get_settings().archive_root)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def _get_jsonl_path(self, trace_id: str) -> Path:
        """Get JSONL file path for a trace_id."""
        # 按 UTC 日期分目录，与报文时间戳时区保持一致
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self.archive_root / date_str / f"{trace_id}.jsonl"

    def append_archive(self, trace_id: str, task_id: Optional[str], envelope: Envelope) -> MessageArchive:
        """
        Append envelope to JSONL file and create MessageArchive index row.
        Returns the created MessageArchive record.
        """
        jsonl_path = self._get_jsonl_path(trace_id)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize envelope to JSON
        raw_json = envelope.model_dump_json()

        # Append to JSONL file
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(raw_json + "\n")

        # Create MessageArchive index record
        archive_record = MessageArchive(
            trace_id=trace_id,
            task_id=task_id,
            msg_type=envelope.msg_type.value,
            sender=envelope.sender.value,
            timestamp=envelope.timestamp,
            raw_json=raw_json,
        )

        self.db.add(archive_record)
        self.db.commit()
        self.db.refresh(archive_record)

        return archive_record

    def get_archive_path(self, trace_id: str) -> Optional[Path]:
        """Get the JSONL file path for a trace_id if it exists."""
        jsonl_path = self._get_jsonl_path(trace_id)
        return jsonl_path if jsonl_path.exists() else None
