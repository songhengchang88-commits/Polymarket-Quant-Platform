"""只追加(append-only)事件账本。

为什么不用 print/裸 open('a'): 多线程并发写 JSONL 会互相截断, 事后无法回放。
这里用进程内锁 + fsync, 并给每条记录加 schema 版本与唯一 id, 保证可重放、可审计。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 1


class Journal:
    def __init__(self, path: str, fsync: bool = True):
        self.path = path
        self.fsync = fsync
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def new_id(self, prefix: str = "evt") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """写入一条事件, 返回补全元信息后的记录。"""
        record = dict(event)
        record.setdefault("id", self.new_id(str(record.get("type", "evt")).lower()))
        record.setdefault("ts", round(time.time(), 6))
        record.setdefault("ts_iso", time.strftime("%Y-%m-%d %H:%M:%S"))
        record["schema"] = SCHEMA_VERSION
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                if self.fsync:
                    os.fsync(f.fileno())
        return record

    def read_all(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # 半行写入(进程被杀)不应摧毁整份账本
                    continue
        if limit is not None:
            out = out[-limit:]
        return out

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.read_all())
