"""`you` / `them` 两个模块库共用的 SQLite 样板。

抽出来的只有「怎么开连接」和「怎么自检」这两段——它们在两边逐字相同，
差异只有抛哪个异常、数哪几张表。

**不抽的部分**：两个 store 各自的 schema、各自的库文件、各自的业务方法。
两库分开是有意的（见 `them/store.py` 的模块 docstring）：「关掉 them 不影响
you」要在文件层面成立，共用一个库的话，任何一次 schema 迁移都会同时动到
另一个模块的数据。这里抽的是样板，不是把两个模块并成一个。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Sequence


def connect_module_db(
    path: Path,
    root: Path,
    schema: str,
    *,
    create: bool,
    error_factory: Callable[[str], Exception],
    unavailable_message: str,
) -> sqlite3.Connection:
    """打开模块库；库不存在且不建时抛 FileNotFoundError。

    PRAGMA 三件套是有意的：外键约束打开、journal 用 DELETE 而不是 WAL、
    synchronous=FULL。这三个都换来「进程被杀掉时数据不半截」，代价是慢一点，
    而这两个库都是低频写。
    """
    if not path.exists() and not create:
        raise FileNotFoundError(path)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        if create:
            connection.executescript(schema)
        return connection
    except sqlite3.Error as exc:
        raise error_factory(unavailable_message) from exc


def module_integrity_report(
    path: Path,
    lock: Any,
    connect: Callable[[], sqlite3.Connection],
    tables: Sequence[str],
    state_reader: Callable[[], Any],
) -> dict[str, Any]:
    """quick_check + 各表行数 + 模块开关状态。

    `tables` 只接受调用方写死的字面量元组——它直接拼进 SQL。一旦有人把它
    改成接受外部输入，下面那行 nosec 就会掩盖一个真的注入口。
    """
    if not path.exists():
        return {"ok": True, "exists": False, "enabled": False}
    try:
        with lock:
            connection = connect()
            try:
                check = connection.execute("PRAGMA quick_check").fetchone()
                counts = {
                    table: int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"  # nosec B608
                        ).fetchone()[0]
                    )
                    for table in tables
                }
            finally:
                connection.close()
        state = state_reader()
        return {
            "ok": bool(check and str(check[0]).lower() == "ok"),
            "exists": True,
            "enabled": state.enabled,
            "state_revision": state.state_revision,
            "counts": counts,
        }
    except Exception:
        return {"ok": False, "exists": True, "enabled": False}
