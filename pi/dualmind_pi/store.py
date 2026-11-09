"""Caché en memoria de la telemetría de DynamoDB.

Se cargan las últimas horas una vez y después solo se piden los ítems nuevos
(ts > último visto), para que la interfaz no relea 12 h cada 30 s.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

log = logging.getLogger(__name__)


def _from_ddb(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _from_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_from_ddb(x) for x in v]
    return v


def _to_ddb(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb(x) for x in v]
    return v


class TelemetryStore:
    def __init__(self, table, device_id: str, keep_hours: int = 13):
        self.table = table
        self.device_id = device_id
        self.keep_ms = keep_hours * 3600 * 1000
        self._items: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _query_since(self, since_ms: int) -> list[dict[str, Any]]:
        out, kwargs = [], {
            "KeyConditionExpression": Key("device_id").eq(self.device_id) & Key("ts").gt(since_ms),
        }
        while True:
            resp = self.table.query(**kwargs)
            out.extend(_from_ddb(i) for i in resp["Items"])
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def refresh(self) -> int:
        """Trae los ítems nuevos; regresa cuántos llegaron."""
        now = int(time.time() * 1000)
        with self._lock:
            since = max(self._items) if self._items else now - self.keep_ms
        new = self._query_since(since)
        with self._lock:
            for it in new:
                self._items[int(it["ts"])] = it
            cutoff = now - self.keep_ms
            for ts in [t for t in self._items if t < cutoff]:
                del self._items[ts]
        return len(new)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._items[max(self._items)] if self._items else None

    def since(self, since_ms: int) -> list[dict[str, Any]]:
        with self._lock:
            return [self._items[t] for t in sorted(self._items) if t >= since_ms]

    def save_llm(self, ts: int, llm: dict[str, Any]) -> None:
        self.table.update_item(
            Key={"device_id": self.device_id, "ts": ts},
            UpdateExpression="SET llm = :l",
            ConditionExpression="attribute_exists(device_id)",
            ExpressionAttributeValues={":l": _to_ddb(llm)},
        )
        self.cache_llm(ts, llm)

    def cache_llm(self, ts: int, llm: dict[str, Any]) -> None:
        """Actualiza solo la caché (cuando otro, p. ej. AgentCore, ya escribió en DynamoDB)."""
        with self._lock:
            if ts in self._items:
                self._items[ts]["llm"] = llm
