#!/usr/bin/env python3
"""Database initialization script.
Creates tables and indexes for the Master-Worker Platform.
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Base, PipelineLedger, TaskLedger, MessageArchive


def init_database(demo: bool = False):
    settings = get_settings()
    database_url = settings.database_url
    
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    # Use settings.debug to control echo
    engine = create_engine(database_url, echo=settings.log_level == "DEBUG")
    
    Base.metadata.create_all(engine)
    
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Created tables: {tables}")
    
    for table in tables:
        indexes = inspector.get_indexes(table)
        print(f"Indexes for {table}: {indexes}")
    
    if demo:
        with Session(engine) as session:
            pipeline = PipelineLedger(
                trace_id="test-trace-001",
                original_demand="Test demand",
                overall_status="pending"
            )
            session.add(pipeline)
            session.commit()
            print(f"Inserted PipelineLedger: {pipeline.trace_id}")
            
            task = TaskLedger(
                task_id="test-task-001",
                trace_id="test-trace-001",
                target_worker="dsh",
                status="待拆解",
                max_retry=3,
                remaining_retry=3,
                is_high_risk=False,
                depends_on=[]
            )
            session.add(task)
            session.commit()
            print(f"Inserted TaskLedger: {task.task_id}")
            
            from datetime import datetime, timezone
            from app.schemas import Envelope

            demo_envelope = Envelope(
                trace_id="test-trace-001",
                task_id="test-task-001",
                msg_type="task_dispatch",
                sender="master",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                payload={"task_goal": "demo", "is_high_risk": False},
            )
            archive = MessageArchive(
                trace_id="test-trace-001",
                task_id="test-task-001",
                msg_type=demo_envelope.msg_type.value,
                sender=demo_envelope.sender.value,
                timestamp=demo_envelope.timestamp,
                raw_json=demo_envelope.model_dump_json(),
            )
            session.add(archive)
            session.commit()
            print(f"Inserted MessageArchive: {archive.id}")
            
            pipelines = session.query(PipelineLedger).all()
            tasks = session.query(TaskLedger).all()
            archives = session.query(MessageArchive).all()
            print(f"Pipelines: {len(pipelines)}, Tasks: {len(tasks)}, Archives: {len(archives)}")
    
    print("Database initialization completed successfully!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize database")
    parser.add_argument("--demo", action="store_true", help="Insert demo data (default: False)")
    args = parser.parse_args()
    
    try:
        init_database(demo=args.demo)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
