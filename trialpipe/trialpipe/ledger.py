"""只追加事件台账（append-only ledger）。

设计约束：
  - 事件一经追加不可修改、不可删除；任何"变更"都以新事件表达
  - 每条事件携带前一条事件的 SHA-256，形成哈希链，可整体验真
  - 身份纠错与分组修订必须写明原因，旧值与新值同时保留
  - 台账文件以 fsync 落盘；读取时校验链完整性，发现断裂立即报错

事件类型（type）：
  enrollment      入组登记
  assignment      分组接入（随机化/分配记录）
  id_correction   身份纠错（old_subject_id -> new_subject_id，必须带 reason）
  group_revision  分组记录修订（旧组 -> 新组，必须带 reason，保留修订链）
  withdrawal      退出（迟到退出也只是新事件，不删人）
  outcome         结局（version 区分补录/修订版，as_of 为数据成熟时点）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Optional

EVENT_TYPES = {
    "enrollment", "assignment", "id_correction",
    "group_revision", "withdrawal", "outcome",
}

# 各事件类型的必填字段
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "enrollment": ("subject_id", "enrolled_at"),
    "assignment": ("subject_id", "group", "assigned_at"),
    "id_correction": ("subject_id", "old_subject_id", "new_subject_id",
                      "reason", "corrected_at"),
    "group_revision": ("subject_id", "old_group", "new_group", "reason", "revised_at"),
    "withdrawal": ("subject_id", "withdrawn_at", "reason"),
    "outcome": ("subject_id", "outcome_name", "value", "version", "as_of"),
}


class LedgerError(Exception):
    """台账校验或追加失败。"""


@dataclass
class LedgerState:
    """校验后的台账快照。"""
    events: list[dict]
    head_hash: str
    verified: bool


def canonical_payload(event: dict) -> bytes:
    """事件的规范序列化：固定键序、紧凑分隔，保证哈希可复现。"""
    body = {k: v for k, v in event.items() if k not in ("seq", "hash", "prev_hash")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def event_hash(prev_hash: str, event: dict) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(b"|")
    h.update(canonical_payload(event))
    return h.hexdigest()


class AppendOnlyLedger:
    """文件支持的只追加台账，线程安全（进程内锁）。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8"):
                pass

    # ------------------------------------------------------------------
    def _validate(self, event: dict) -> None:
        etype = event.get("type")
        if etype not in EVENT_TYPES:
            raise LedgerError(f"未知事件类型 {etype!r}")
        for field in REQUIRED_FIELDS[etype]:
            if field not in event or event[field] in (None, ""):
                raise LedgerError(f"事件 {etype} 缺少必填字段 {field!r}")
        if etype == "id_correction":
            if event["old_subject_id"] == event["new_subject_id"]:
                raise LedgerError("身份纠错的新旧 ID 不能相同")
        if etype == "group_revision":
            if event["old_group"] == event["new_group"]:
                raise LedgerError("分组修订的新旧分组不能相同")
        if etype == "outcome":
            if not isinstance(event["version"], int) or event["version"] < 1:
                raise LedgerError("结局 version 必须是 >=1 的整数")

    def append(self, event: dict) -> dict:
        """校验并追加事件，返回落盘后的完整事件（含 seq/prev_hash/hash）。

        写盘前做一次试重放：语义错误（重复入组、撞 ID 合并、修订前提
        不成立、结局版本冲突等）在落盘前拒绝，保证坏事件无法毒化台账。
        """
        event = dict(event)
        self._validate(event)
        with self._lock:
            state = self._read_locked(verify=True)
            # 试重放：SubjectView 对全量事件（含待写入事件）做语义校验
            SubjectView(state.events + [event])
            seq = len(state.events)
            stored = dict(event)
            stored["seq"] = seq
            stored["prev_hash"] = state.head_hash
            stored["hash"] = event_hash(state.head_hash, stored)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(stored, sort_keys=True,
                                    ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return stored

    # ------------------------------------------------------------------
    def _read_locked(self, verify: bool = True) -> LedgerState:
        events: list[dict] = []
        prev = "0" * 64
        if not os.path.exists(self.path):
            return LedgerState([], prev, True)
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"第 {lineno} 行不是合法 JSON: {exc}") from exc
                if verify:
                    if event.get("prev_hash") != prev:
                        raise LedgerError(
                            f"第 {lineno} 行哈希链断裂：prev_hash 不匹配")
                    expected = event_hash(prev, event)
                    if event.get("hash") != expected:
                        raise LedgerError(
                            f"第 {lineno} 行内容哈希不匹配，台账可能被篡改")
                    if event.get("seq") != lineno - 1:
                        raise LedgerError(f"第 {lineno} 行序号不连续")
                prev = event.get("hash", prev)
                events.append(event)
        return LedgerState(events, prev, verify)

    def read(self, verify: bool = True) -> LedgerState:
        with self._lock:
            return self._read_locked(verify=verify)

    def head_hash(self) -> str:
        return self.read().head_hash


class SubjectView:
    """把只追加事件流投影为受试者当前/历史状态（纯内存、可重放）。"""

    # 各类事件的生效时间戳字段（用于 as-of 时点穿越）
    _TS_FIELD = {
        "enrollment": "enrolled_at",
        "assignment": "assigned_at",
        "id_correction": "corrected_at",
        "group_revision": "revised_at",
        "withdrawal": "withdrawn_at",
        "outcome": "as_of",
    }

    def __init__(self, events: list[dict]):
        self.events = events
        self.aliases: dict[str, str] = {}   # 旧 ID -> 当前 ID
        self.enrolled: dict[str, dict] = {}
        self.assignments: dict[str, list[dict]] = {}
        self.withdrawals: dict[str, dict] = {}
        self.outcomes: dict[tuple[str, str, int], dict] = {}
        self.corrections: list[dict] = []
        self._replay()

    @classmethod
    def events_as_of(cls, events: list[dict], as_of: str) -> list[dict]:
        """只保留生效时点 <= as_of 的事件，实现严格时点穿越。

        晚于 as_of 的身份纠错/分组修订/退出/结局一律不进入该时点视图，
        因此同一 as_of 的冻结结果不会被后补事件改变。
        """
        kept = []
        for ev in events:
            ts = ev.get(cls._TS_FIELD.get(ev["type"], ""))
            if ts is not None and ts <= as_of:
                kept.append(ev)
        return kept

    def _resolve(self, subject_id: str) -> str:
        seen = set()
        cur = subject_id
        while cur in self.aliases and cur not in seen:
            seen.add(cur)
            cur = self.aliases[cur]
        return cur

    def _replay(self) -> None:
        for ev in self.events:
            etype = ev["type"]
            if etype == "enrollment":
                sid = ev["subject_id"]
                if sid in self.enrolled:
                    raise LedgerError(f"受试者 {sid} 重复入组")
                self.enrolled[sid] = ev
            elif etype == "id_correction":
                old, new = ev["old_subject_id"], ev["new_subject_id"]
                if old not in self.enrolled:
                    raise LedgerError(
                        f"身份纠错对象 {old} 尚未入组，不能纠错")
                if new in self.enrolled:
                    raise LedgerError(f"新 ID {new} 已被占用，拒绝合并")
                # 把旧 ID 名下的全部状态迁移到新 ID
                self.enrolled[new] = self.enrolled.pop(old)
                if old in self.assignments:
                    self.assignments[new] = self.assignments.pop(old)
                if old in self.withdrawals:
                    self.withdrawals[new] = self.withdrawals.pop(old)
                # 结局键迁移
                for key in [k for k in self.outcomes if k[0] == old]:
                    self.outcomes[(new, key[1], key[2])] = self.outcomes.pop(key)
                self.aliases[old] = new
                self.corrections.append(ev)
            elif etype == "assignment":
                sid = self._resolve(ev["subject_id"])
                self.assignments.setdefault(sid, []).append(ev)
            elif etype == "group_revision":
                sid = self._resolve(ev["subject_id"])
                history = self.assignments.setdefault(sid, [])
                current = None
                for past in history:
                    current = (past["group"] if past["type"] == "assignment"
                               else past["new_group"])
                if not history:
                    raise LedgerError(
                        f"受试者 {sid} 尚无分组记录，不能修订")
                if ev["old_group"] != current:
                    raise LedgerError(
                        f"受试者 {sid} 分组修订前提不成立：当前 {current!r}，"
                        f"修订声称旧值 {ev['old_group']!r}")
                history.append(ev)
            elif etype == "withdrawal":
                sid = self._resolve(ev["subject_id"])
                self.withdrawals[sid] = ev
            elif etype == "outcome":
                sid = self._resolve(ev["subject_id"])
                key = (sid, ev["outcome_name"], int(ev["version"]))
                if key in self.outcomes:
                    raise LedgerError(
                        f"结局 {ev['outcome_name']} v{ev['version']} 已存在，"
                        "修订必须升版本号")
                self.outcomes[key] = ev

    # ------------------------------------------------------------------
    def current_id(self, subject_id: str) -> str:
        return self._resolve(subject_id)

    def current_group(self, subject_id: str) -> Optional[str]:
        sid = self._resolve(subject_id)
        history = self.assignments.get(sid, [])
        group = None
        for ev in history:
            if ev["type"] == "assignment":
                group = ev["group"]
            elif ev["type"] == "group_revision":
                if ev["old_group"] != group:
                    raise LedgerError(
                        f"受试者 {sid} 分组修订前提不成立："
                        f"当前 {group!r}，修订声称旧值 {ev['old_group']!r}")
                group = ev["new_group"]
        return group

    def group_at(self, subject_id: str, as_of: str) -> Optional[str]:
        """as-of 时点（ISO 时间戳字典序可比）的分组，历史可还原。"""
        sid = self._resolve(subject_id)
        group = None
        for ev in self.assignments.get(sid, []):
            ts = ev.get("assigned_at") or ev.get("revised_at")
            if ts is not None and ts <= as_of:
                if ev["type"] == "assignment":
                    group = ev["group"]
                else:
                    group = ev["new_group"]
        return group

    def withdrawn_at(self, subject_id: str) -> Optional[str]:
        sid = self._resolve(subject_id)
        ev = self.withdrawals.get(sid)
        return ev["withdrawn_at"] if ev else None

    def all_current_ids(self) -> list[str]:
        return sorted(self.enrolled.keys())
