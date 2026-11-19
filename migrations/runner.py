"""极简增量迁移框架（零第三方依赖）。

## 为什么不用 Alembic

- 底座原则是"零重依赖"：Windows / Mac 双端一条命令就能跑，不引入新的安装面。
- Mac 侧 pip 镜像经常 429，Alembic 的安装链（alembic + Mako + markupSafe）会把"一键迁移"直接挂死。
- 我们的迁移量级很小：单库、单线、无分支、无 merge。Alembic 的价值（autogenerate、多分支、离线 SQL）
  在这里换不回等价的回报，却带来一套额外的 env.py / alembic.ini 维护面。
- 真要上 Alembic 时，`schema_version` 表就是现成的版本锚点，可以平移过去（见 docs/base/12）。

## 约定

- 每个迁移是 `migrations/versions/vNNN_xxx.py`，NNN 为三位数字，**唯一且递增**。
- 模块必须导出：
  - `VERSION`：int，与文件名前缀一致
  - `NOTE`：str，一句话说明这次改了什么
  - `up(conn)`：接收一个 SQLAlchemy Connection，在**调用方事务内**执行
- 可选的 `down(conn)` 只作书面参考：本框架**不提供自动回滚**（数据迁移没有安全回滚，见 docs/base/12）。
"""
import importlib.util
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy import Engine, inspect, text

DEFAULT_VERSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "versions")

FILENAME_RE = re.compile(r"^v(\d{3})_([A-Za-z0-9_]+)\.py$")

VERSION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at VARCHAR(64) NOT NULL,
    note VARCHAR(255)
)
"""


class MigrationError(RuntimeError):
    """迁移目录本身有问题（版本重复、模块缺字段等），不是数据库问题。"""


@dataclass(frozen=True)
class Migration:
    version: int
    path: str
    note: str
    up: Callable


def _load_module(path: str):
    name = "mwp_migration_" + os.path.basename(path).replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"无法加载迁移文件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(versions_dir: Optional[str] = None) -> List[Migration]:
    """扫描并校验迁移文件，返回按版本升序的列表。"""
    vdir = versions_dir or DEFAULT_VERSIONS_DIR
    if not os.path.isdir(vdir):
        raise MigrationError(f"迁移目录不存在：{vdir}")

    found: List[Migration] = []
    seen: Dict[int, str] = {}
    for fname in sorted(os.listdir(vdir)):
        if fname.startswith("__") or not fname.endswith(".py"):
            continue
        m = FILENAME_RE.match(fname)
        if not m:
            continue  # 不符合命名约定的文件不参与（例如 __init__.py）
        file_version = int(m.group(1))
        path = os.path.join(vdir, fname)
        module = _load_module(path)

        for attr in ("VERSION", "NOTE", "up"):
            if not hasattr(module, attr):
                raise MigrationError(f"{fname} 缺少必需属性：{attr}")
        declared = int(module.VERSION)
        if declared != file_version:
            raise MigrationError(f"{fname} 的 VERSION={declared} 与文件名前缀 {file_version} 不一致")
        if declared in seen:
            raise MigrationError(f"版本号重复：v{declared:03d} 同时出现在 {seen[declared]} 和 {fname}")
        seen[declared] = fname
        if not callable(module.up):
            raise MigrationError(f"{fname} 的 up 不是可调用对象")
        found.append(Migration(version=declared, path=path, note=str(module.NOTE), up=module.up))

    found.sort(key=lambda x: x.version)
    return found


def ensure_version_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(VERSION_TABLE_DDL))


def applied_versions(engine: Engine) -> Dict[int, str]:
    """返回 {version: applied_at}。表不存在时返回空字典（不建表）。"""
    if "schema_version" not in inspect(engine).get_table_names():
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version, applied_at FROM schema_version")).all()
    return {int(r[0]): str(r[1]) for r in rows}


def current_version(engine: Engine) -> int:
    applied = applied_versions(engine)
    return max(applied) if applied else 0


def latest_available(versions_dir: Optional[str] = None) -> int:
    found = discover(versions_dir)
    return found[-1].version if found else 0


def pending(engine: Engine, versions_dir: Optional[str] = None) -> List[Migration]:
    applied = applied_versions(engine)
    return [m for m in discover(versions_dir) if m.version not in applied]


def upgrade(
    engine: Engine,
    versions_dir: Optional[str] = None,
    target: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
) -> List[Migration]:
    """按版本顺序应用未执行的迁移。每个迁移在独立事务内执行，失败即中断并抛出。

    返回本次实际应用的迁移列表。已应用过的版本会被跳过（幂等）。
    """
    say = log or (lambda _msg: None)
    ensure_version_table(engine)
    todo = pending(engine, versions_dir)
    if target is not None:
        todo = [m for m in todo if m.version <= target]

    done: List[Migration] = []
    for mig in todo:
        say(f"→ 应用 v{mig.version:03d}：{mig.note}")
        now = datetime.now(timezone.utc).isoformat()
        try:
            with engine.begin() as conn:
                mig.up(conn)
                conn.execute(
                    text("INSERT INTO schema_version (version, applied_at, note) VALUES (:v, :a, :n)"),
                    {"v": mig.version, "a": now, "n": mig.note},
                )
        except Exception:
            say(f"  ✗ v{mig.version:03d} 失败，已回滚到执行前状态")
            raise
        done.append(mig)
    return done


def status_rows(engine: Engine, versions_dir: Optional[str] = None) -> List[dict]:
    applied = applied_versions(engine)
    rows = []
    for mig in discover(versions_dir):
        rows.append(
            {
                "version": mig.version,
                "note": mig.note,
                "applied": mig.version in applied,
                "applied_at": applied.get(mig.version, ""),
            }
        )
    return rows
