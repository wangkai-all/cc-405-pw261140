"""样本冻结器：在给定 as-of 时点生成不可变样本清单（frozen manifest）。

核心保证：
  - 冻结只依赖当时的台账事件；冻结后再追加的任何事件（迟到退出、
    分组修订、补结局）都不能改变这份清单的成员与分组
  - 清单逐人记录纳入/排除理由；身份纠错按冻结时的别名映射固定 ID
  - 清单内容整体哈希并落盘，重复冻结同一时点必须字节级一致
  - 结局数据在冻结之后才成熟：冻结清单里没有结局字段，杜绝"看着
    结局挑样本"
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from .ledger import AppendOnlyLedger, LedgerError, SubjectView


class FreezeError(Exception):
    pass


@dataclass
class FreezeCriteria:
    """冻结标准。as_of 为闭区间上界（ISO 时间戳）。"""
    as_of: str
    study_id: str
    groups: tuple[str, ...]
    # 退出时点 <= as_of 者排除；> as_of 的迟到退出不影响本清单
    exclude_withdrawn_by_as_of: bool = True
    # 无分组记录者排除（ITT 人群要求分组先于冻结）
    require_assignment: bool = True

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "study_id": self.study_id,
            "groups": list(self.groups),
            "exclude_withdrawn_by_as_of": self.exclude_withdrawn_by_as_of,
            "require_assignment": self.require_assignment,
        }


def _manifest_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class SampleFreezer:
    def __init__(self, ledger: AppendOnlyLedger, store_dir: str):
        self.ledger = ledger
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def _path_for(self, manifest_id: str) -> str:
        return os.path.join(self.store_dir, f"{manifest_id}.json")

    def build_manifest(self, criteria: FreezeCriteria) -> dict:
        """根据台账当前内容构建清单（不落盘）。

        身份只由 as-of 时点可见事件决定：晚于 as_of 的纠错/修订/退出/
        结局被排除在投影之外，保证同一 as_of 反复冻结身份不变。
        """
        state = self.ledger.read(verify=True)
        # 严格时点穿越：只用不晚于 as_of 的事件重建视图
        as_of_events = SubjectView.events_as_of(state.events, criteria.as_of)
        view = SubjectView(as_of_events)

        members: list[dict] = []
        exclusions: list[dict] = []
        for original_id in sorted(view.enrolled, key=lambda x: x):
            current_id = view.current_id(original_id)
            enrolled_at = view.enrolled[original_id]["enrolled_at"]
            if enrolled_at > criteria.as_of:
                exclusions.append({
                    "subject_id": current_id,
                    "original_id": original_id,
                    "reason": "enrolled_after_as_of",
                    "detail": f"入组时点 {enrolled_at} 晚于冻结时点 {criteria.as_of}",
                })
                continue

            withdrawn = view.withdrawn_at(original_id)
            if (criteria.exclude_withdrawn_by_as_of and withdrawn
                    and withdrawn <= criteria.as_of):
                exclusions.append({
                    "subject_id": current_id,
                    "original_id": original_id,
                    "reason": "withdrawn_by_as_of",
                    "detail": f"退出时点 {withdrawn} 不晚于冻结时点",
                })
                continue

            group = view.group_at(original_id, criteria.as_of)
            if criteria.require_assignment and group is None:
                exclusions.append({
                    "subject_id": current_id,
                    "original_id": original_id,
                    "reason": "no_assignment_by_as_of",
                    "detail": "冻结时点前没有有效分组记录",
                })
                continue
            if group is not None and group not in criteria.groups:
                exclusions.append({
                    "subject_id": current_id,
                    "original_id": original_id,
                    "reason": "group_out_of_scope",
                    "detail": f"分组 {group!r} 不在分析分组 {list(criteria.groups)} 内",
                })
                continue

            members.append({
                "subject_id": current_id,
                "id_at_enrollment": original_id,
                "group_frozen": group,
                "enrolled_at": enrolled_at,
                "withdrawn_at_freeze": withdrawn,
                "id_aliases_frozen": [
                    old for old, new in view.aliases.items()
                    if view.current_id(old) == current_id
                ],
            })

        members.sort(key=lambda m: m["subject_id"])
        exclusions.sort(key=lambda e: (e["subject_id"], e["reason"]))
        counts: dict[str, int] = {}
        for m in members:
            counts[m["group_frozen"]] = counts.get(m["group_frozen"], 0) + 1

        # 身份内容：只含 as-of 时点决定的东西；链头/事件数是溯源，不参与身份
        identity = {
            "manifest_kind": "frozen_sample_manifest",
            "study_id": criteria.study_id,
            "criteria": criteria.to_dict(),
            "members": members,
            "exclusions": exclusions,
            "group_counts": counts,
            "n_members": len(members),
        }
        manifest_id = _manifest_hash(identity)[:16]

        manifest = dict(identity)
        manifest["manifest_id"] = manifest_id
        # 溯源信息：冻结时台账链头与可见事件数，便于审计但不影响身份
        manifest["provenance"] = {
            "ledger_head_hash": state.head_hash,
            "event_count_total": len(state.events),
            "event_count_visible_at_as_of": len(as_of_events),
        }
        manifest["ledger_head_hash"] = state.head_hash
        manifest["event_count"] = len(as_of_events)
        # 整文件哈希：覆盖含溯源在内的全部落盘内容，任何篡改都可检出
        manifest["manifest_hash"] = _manifest_hash(manifest)
        return manifest

    def freeze(self, criteria: FreezeCriteria,
               manifest_id: Optional[str] = None) -> dict:
        """构建并持久化清单。

        同一 as_of 标准的身份是确定的：若同 ID 文件已存在，直接返回那份
        不可变文件，绝不用新链头覆盖它（溯源保留首次冻结时的记录）。
        """
        manifest = self.build_manifest(criteria)
        if manifest_id is not None and manifest_id != manifest["manifest_id"]:
            raise FreezeError(
                f"指定的 manifest_id {manifest_id} 与计算结果 "
                f"{manifest['manifest_id']} 不一致，拒绝写入")
        path = self._path_for(manifest["manifest_id"])
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            # 身份必须逐字段一致；溯源允许不同（链头会前进），但文件不覆盖
            identity_fields = ("study_id", "criteria", "members",
                               "exclusions", "group_counts", "n_members")
            for field in identity_fields:
                if existing.get(field) != manifest.get(field):
                    raise FreezeError(
                        f"清单 ID 冲突但身份内容不同：{manifest['manifest_id']}")
            return existing  # 幂等：同一冻结身份返回同一不可变文件
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return manifest

    def load(self, manifest_id: str, verify: bool = True) -> dict:
        path = self._path_for(manifest_id)
        if not os.path.exists(path):
            raise FreezeError(f"清单 {manifest_id} 不存在")
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        if verify:
            stored_hash = manifest.get("manifest_hash")
            recomputed = _manifest_hash(
                {k: v for k, v in manifest.items()
                 if k not in ("manifest_hash",)})
            if stored_hash != recomputed:
                raise FreezeError(f"清单 {manifest_id} 哈希校验失败")
        return manifest

    def list_manifests(self) -> list[dict]:
        out = []
        if not os.path.isdir(self.store_dir):
            return out
        for name in sorted(os.listdir(self.store_dir)):
            if name.endswith(".json"):
                with open(os.path.join(self.store_dir, name),
                          "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                out.append({
                    "manifest_id": m["manifest_id"],
                    "study_id": m["study_id"],
                    "as_of": m["criteria"]["as_of"],
                    "n_members": m["n_members"],
                    "group_counts": m["group_counts"],
                    "ledger_head_hash": m["ledger_head_hash"],
                })
        return out
