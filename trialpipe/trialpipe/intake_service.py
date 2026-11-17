"""分组接入服务（intake, :8101）。

只负责把事件追加进哈希链台账，并提供只读查询/验真。
它不做样本冻结、不做统计——保证"接入"与"分析"物理分离。
"""

from __future__ import annotations

import json

from .http_common import JsonHandler
from .ledger import EVENT_TYPES, AppendOnlyLedger, SubjectView


class IntakeHandler(JsonHandler):
    def handle_get(self) -> None:
        ledger: AppendOnlyLedger = self.stores["ledger"]
        path = self._path()
        if path == "/health":
            self._send_json({"service": "intake", "status": "ok"})
        elif path == "/events":
            state = ledger.read(verify=True)
            self._send_json({
                "event_count": len(state.events),
                "head_hash": state.head_hash,
                "verified": state.verified,
                "events": state.events,
            })
        elif path.startswith("/subjects/"):
            sid = path.split("/", 2)[2]
            state = ledger.read(verify=True)
            view = SubjectView(state.events)
            cur = view.current_id(sid)
            if cur not in view.enrolled:
                self._send_json({"error": f"受试者 {sid} 不存在"}, 404)
                return
            related = [e for e in state.events
                       if view.current_id(e.get("subject_id", cur)) == cur
                       or e.get("old_subject_id") == sid
                       or e.get("new_subject_id") == cur]
            self._send_json({
                "query_id": sid,
                "current_id": cur,
                "current_group": view.current_group(cur),
                "withdrawn_at": view.withdrawn_at(cur),
                "events": related,
            })
        elif path == "/verify":
            state = ledger.read(verify=True)
            self._send_json({
                "verified": True,
                "event_count": len(state.events),
                "head_hash": state.head_hash,
            })
        else:
            self._send_json({"error": "未知路径"}, 404)

    def handle_post(self) -> None:
        ledger: AppendOnlyLedger = self.stores["ledger"]
        path = self._path()
        if path == "/events":
            event = self._read_json()
            stored = ledger.append(event)
            self._send_json({"accepted": True, "event": stored}, 201)
        elif path == "/events/batch":
            payload = self._read_json()
            events = payload.get("events")
            if not isinstance(events, list) or not events:
                raise ValueError("batch 需要非空 events 数组")
            stored_all = []
            for ev in events:
                if not isinstance(ev, dict):
                    raise ValueError("events 元素必须是对象")
                stored_all.append(ledger.append(ev))
            self._send_json({"accepted": len(stored_all),
                             "events": stored_all}, 201)
        else:
            self._send_json({"error": "未知路径"}, 404)


def build(host: str, port: int, ledger_path: str):
    from .http_common import make_server
    ledger = AppendOnlyLedger(ledger_path)
    return make_server(host, port, IntakeHandler, {"ledger": ledger})
