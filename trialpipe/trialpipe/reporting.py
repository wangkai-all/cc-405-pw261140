"""统计报告服务。

职责与边界：
  - 报告必须绑定一份已冻结样本清单；分析取数只在该清单成员范围内，
    台账之后追加的迟到退出/分组修订/新入组都无法改变报告人群
  - 结局按 (outcome_name, version) 分版：同一冻结人群可挂不同版本
    结局数据出多版报告，每版报告各自完整、可独立还原
  - 另起一份新清单 = 新人群；两份报告只允许"并列展示比较前提"，
    禁止把不同人群的点估计直接相减并宣称效应
  - 报告状态机：draft -> approved（可中断回 interrupted，可重跑）；
    批准后不可改，新版报告使旧版变为 superseded，但旧文件永不删除，
    其原始输入快照可逐字节还原
  - 计算中断（模拟进程崩溃/超时）只留下 interrupted 标记，不产生
    半截结果；重跑从输入快照重新确定性计算
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from . import stats as stats_mod
from .freezer import SampleFreezer
from .ledger import AppendOnlyLedger, LedgerError, SubjectView

REPORT_STATUSES = ("draft", "interrupted", "approved", "superseded")


class ReportError(Exception):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


@dataclass
class AnalysisConfig:
    outcome_name: str
    outcome_version: int
    outcome_type: str          # binary | continuous
    treatment: str
    control: str
    alpha: float = 0.05
    bootstrap: bool = False
    bootstrap_seed: int = 20261117
    bootstrap_reps: int = 2000

    def to_dict(self) -> dict:
        return {
            "outcome_name": self.outcome_name,
            "outcome_version": self.outcome_version,
            "outcome_type": self.outcome_type,
            "treatment": self.treatment,
            "control": self.control,
            "alpha": self.alpha,
            "bootstrap": self.bootstrap,
            "bootstrap_seed": self.bootstrap_seed,
            "bootstrap_reps": self.bootstrap_reps,
        }


class ReportingService:
    def __init__(self, ledger: AppendOnlyLedger, freezer: SampleFreezer,
                 store_dir: str):
        self.ledger = ledger
        self.freezer = freezer
        self.store_dir = store_dir
        self.input_dir = os.path.join(store_dir, "inputs")
        os.makedirs(self.input_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def _meta_path(self, report_id: str) -> str:
        return os.path.join(self.store_dir, f"{report_id}.json")

    def _input_path(self, input_hash: str) -> str:
        return os.path.join(self.input_dir, f"{input_hash}.jsonl")

    def list_reports(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.store_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.store_dir, name),
                      "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            out.append({k: meta[k] for k in (
                "report_id", "status", "manifest_id", "study_id",
                "outcome_name", "outcome_version", "created_at",
                "approved_at", "superseded_by", "population_fingerprint",
                "n_analyzed", "n_missing_outcomes")
                if k in meta})
        return out

    def load_report(self, report_id: str) -> dict:
        path = self._meta_path(report_id)
        if not os.path.exists(path):
            raise ReportError(f"报告 {report_id} 不存在")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------
    def _collect_inputs(self, manifest: dict, config: AnalysisConfig) -> dict:
        """只从冻结清单成员 + 指定结局版本抽取分析输入。

        后补事件不能影响人群：成员集合完全来自 manifest；
        结局缺失逐人记录，不静默剔除也不臆造填补。
        """
        state = self.ledger.read(verify=True)
        view = SubjectView(state.events)
        wanted_version = int(config.outcome_version)

        rows: list[dict] = []
        missing: list[dict] = []
        for member in manifest["members"]:
            frozen_sid = member["subject_id"]
            # 分组以冻结清单为准，不读当前台账分组
            group = member["group_frozen"]
            # 身份可能在冻结后被纠错：通过当前别名解析定位结局事件，
            # 但行内同时保留冻结时 ID，保证人群口径仍以清单为准
            lookup_sid = view.current_id(frozen_sid)
            key = (lookup_sid, config.outcome_name, wanted_version)
            outcome = view.outcomes.get(key)
            if outcome is None:
                missing.append({
                    "subject_id": frozen_sid,
                    "resolved_id": lookup_sid,
                    "group": group,
                    "reason": f"缺少结局 {config.outcome_name} v{wanted_version}",
                })
                continue
            rows.append({
                "subject_id": frozen_sid,
                "resolved_id": lookup_sid,
                "id_changed_after_freeze": lookup_sid != frozen_sid,
                "group": group,
                "value": outcome["value"],
                "outcome_event_seq": outcome["seq"],
                "outcome_as_of": outcome["as_of"],
            })

        rows.sort(key=lambda r: r["subject_id"])
        missing.sort(key=lambda r: r["subject_id"])
        snapshot = {
            "manifest_id": manifest["manifest_id"],
            "manifest_hash": manifest["manifest_hash"],
            "ledger_head_hash_at_build": state.head_hash,
            "config": config.to_dict(),
            "frozen_members": [
                {"subject_id": m["subject_id"], "group_frozen": m["group_frozen"]}
                for m in manifest["members"]
            ],
            "analysis_rows": rows,
            "missing_outcomes": missing,
        }
        return snapshot

    def _persist_input_snapshot(self, snapshot: dict) -> str:
        """逐行 JSONL 落盘输入快照，返回内容哈希；同内容幂等。"""
        body = "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
            for row in snapshot["analysis_rows"])
        header = {k: v for k, v in snapshot.items() if k != "analysis_rows"}
        blob = (json.dumps(header, sort_keys=True, ensure_ascii=False) + "\n"
                + body).encode("utf-8")
        digest = _sha256_bytes(blob)
        path = self._input_path(digest)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        return digest

    # ------------------------------------------------------------------
    def create_report(self, manifest_id: str, config: AnalysisConfig,
                      created_at: str,
                      simulate_interruption: bool = False) -> dict:
        """创建报告。simulate_interruption=True 演示计算中断：

        输入快照照常落盘（可复算），但报告停在 interrupted，无结果段。
        """
        manifest = self.freezer.load(manifest_id)
        if config.treatment not in manifest["group_counts"] or \
                config.control not in manifest["group_counts"]:
            raise ReportError(
                f"冻结人群中找不到分组 "
                f"{config.treatment!r}/{config.control!r}，"
                f"可用分组：{list(manifest['group_counts'])}")
        snapshot = self._collect_inputs(manifest, config)
        input_hash = self._persist_input_snapshot(snapshot)

        population_fingerprint = _sha256_bytes(_canonical({
            "manifest_id": manifest["manifest_id"],
            "members": [(m["subject_id"], m["group_frozen"])
                        for m in manifest["members"]],
        }))

        report_id = _sha256_bytes(_canonical({
            "manifest_id": manifest["manifest_id"],
            "config": config.to_dict(),
            "input_hash": input_hash,
        }))[:16]

        # 幂等保护：同 (清单, 配置, 输入) 必然得到同 ID。
        # 若报告已存在，原样返回，绝不用新状态覆盖已批准/已中断的报告。
        existing_path = self._meta_path(report_id)
        if os.path.exists(existing_path):
            return self.load_report(report_id)

        meta = {
            "report_id": report_id,
            "study_id": manifest["study_id"],
            "status": "draft",
            "manifest_id": manifest_id,
            "manifest_hash": manifest["manifest_hash"],
            "population_fingerprint": population_fingerprint,
            "input_snapshot_hash": input_hash,
            "config": config.to_dict(),
            "outcome_name": config.outcome_name,
            "outcome_version": config.outcome_version,
            "created_at": created_at,
            "approved_at": None,
            "superseded_by": None,
            "interruption": None,
            "result": None,
            "n_analyzed": len(snapshot["analysis_rows"]),
            "n_missing_outcomes": len(snapshot["missing_outcomes"]),
        }

        if simulate_interruption:
            meta["status"] = "interrupted"
            meta["interruption"] = {
                "stage": "statistical_computation",
                "reason": "模拟计算中断（进程在结果写出前终止）",
                "at": created_at,
                "resumable": True,
                "note": "输入快照已固定，重跑将得到与未中断完全一致的结果",
            }
            self._write_meta(report_id, meta)
            return meta

        result = stats_mod.analyze(snapshot["analysis_rows"], config.to_dict())
        meta["result"] = result
        meta["status"] = "draft"
        self._write_meta(report_id, meta)
        return meta

    def resume_report(self, report_id: str, resumed_at: str) -> dict:
        """从中断恢复：基于固定输入快照重新计算，结果确定性可复现。"""
        meta = self.load_report(report_id)
        if meta["status"] != "interrupted":
            raise ReportError(
                f"报告 {report_id} 状态为 {meta['status']}，无需恢复")
        snapshot = self.restore_input_snapshot(report_id)
        result = stats_mod.analyze(
            snapshot["analysis_rows"], snapshot["config"])
        meta["result"] = result
        meta["status"] = "draft"
        meta["interruption"] = {
            **meta["interruption"],
            "resumed_at": resumed_at,
            "resumed": True,
        }
        self._write_meta(report_id, meta)
        return meta

    def approve(self, report_id: str, approved_at: str,
                approver: str = "statistician") -> dict:
        meta = self.load_report(report_id)
        if meta["status"] not in ("draft",):
            raise ReportError(
                f"只有 draft 报告可批准，当前状态 {meta['status']}")
        if meta.get("result") is None:
            raise ReportError("报告没有有效结果，不能批准")
        meta["status"] = "approved"
        meta["approved_at"] = approved_at
        meta["approver"] = approver
        self._write_meta(report_id, meta)
        return meta

    def supersede(self, old_report_id: str, new_report_id: str) -> dict:
        """新报告批准后，把同人群旧版报告标记为 superseded（文件保留）。"""
        old = self.load_report(old_report_id)
        new = self.load_report(new_report_id)
        if new["status"] != "approved":
            raise ReportError("只有已批准的新报告才能作废旧报告")
        if old["population_fingerprint"] != new["population_fingerprint"]:
            raise ReportError(
                "人群指纹不同，不能作废旧报告——这是两份不同人群的报告，"
                "应并列展示比较前提，而非版本更替")
        old["status"] = "superseded"
        old["superseded_by"] = new_report_id
        self._write_meta(old_report_id, old)
        return old

    # ------------------------------------------------------------------
    def restore_input_snapshot(self, report_id: str) -> dict:
        """还原报告的原始输入（首行为头 JSON，其后为逐样本 JSONL）。"""
        meta = self.load_report(report_id)
        path = self._input_path(meta["input_snapshot_hash"])
        if not os.path.exists(path):
            raise ReportError("输入快照文件缺失，无法还原")
        with open(path, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        header = lines[0]
        header["analysis_rows"] = lines[1:]
        # 与落盘前哈希核对
        body = "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
            for row in header["analysis_rows"])
        header_only = {k: v for k, v in header.items() if k != "analysis_rows"}
        blob = (json.dumps(header_only, sort_keys=True, ensure_ascii=False) + "\n"
                + body).encode("utf-8")
        if _sha256_bytes(blob) != meta["input_snapshot_hash"]:
            raise ReportError("输入快照还原后哈希不一致")
        return header

    def per_subject_explanation(self, report_id: str) -> list[dict]:
        """逐样本解释：每人来自哪个清单、哪条结局事件、如何进入统计量。"""
        meta = self.load_report(report_id)
        snapshot = self.restore_input_snapshot(report_id)
        cfg = snapshot["config"]
        rows = snapshot["analysis_rows"]
        by_group: dict[str, list[dict]] = {}
        for row in rows:
            by_group.setdefault(row["group"], []).append(row)

        explanations = []
        for row in rows:
            group_rows = by_group[row["group"]]
            n = len(group_rows)
            if cfg["outcome_type"] == "continuous":
                vals = [float(r["value"]) for r in group_rows]
                mean = sum(vals) / n
                sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 \
                    if n >= 2 else float("nan")
                contribution = (
                    f"取值 {row['value']} 计入 {row['group']} 组均值"
                    f"（n={n}，组均值 {mean:.4f}，SD {sd:.4f}）；"
                    f"Welch 区间中该样本通过组均值与样本方差参与 SE 计算")
            else:
                k = sum(int(r["value"]) for r in group_rows)
                contribution = (
                    f"取值 {int(row['value'])} 计入 {row['group']} 组 "
                    f"{k}/{n} 的事件计数；Wilson 区间与 Newcombe "
                    "差值区间均由该计数与 n 计算")
            explanations.append({
                "subject_id": row["subject_id"],
                "group": row["group"],
                "value": row["value"],
                "outcome_event_seq": row["outcome_event_seq"],
                "outcome_as_of": row["outcome_as_of"],
                "manifest_id": snapshot["manifest_id"],
                "frozen_population": True,
                "explanation": contribution,
            })
        for miss in snapshot["missing_outcomes"]:
            explanations.append({
                "subject_id": miss["subject_id"],
                "group": miss["group"],
                "value": None,
                "outcome_event_seq": None,
                "outcome_as_of": None,
                "manifest_id": snapshot["manifest_id"],
                "frozen_population": True,
                "explanation": miss["reason"] + "；该样本保留在冻结人群中，"
                               "但不进入本次区间计算（未做任何插补）",
            })
        explanations.sort(key=lambda e: (e["subject_id"]))
        return explanations

    # ------------------------------------------------------------------
    def compare_reports(self, report_id_a: str, report_id_b: str) -> dict:
        """跨报告比较前提检查。

        同人群：允许并列各版结果，并明确标注是同一冻结人群的不同结局版本。
        不同人群：拒绝直接相减，只返回结构化比较前提。
        """
        a, b = self.load_report(report_id_a), self.load_report(report_id_b)
        same_pop = a["population_fingerprint"] == b["population_fingerprint"]
        premise = {
            "report_a": {
                "report_id": report_id_a,
                "manifest_id": a["manifest_id"],
                "outcome_name": a["outcome_name"],
                "outcome_version": a["outcome_version"],
                "status": a["status"],
                "n_analyzed": a["n_analyzed"],
            },
            "report_b": {
                "report_id": report_id_b,
                "manifest_id": b["manifest_id"],
                "outcome_name": b["outcome_name"],
                "outcome_version": b["outcome_version"],
                "status": b["status"],
                "n_analyzed": b["n_analyzed"],
            },
            "same_frozen_population": same_pop,
            "direct_subtraction_allowed": False,
        }
        if same_pop:
            premise["comparison_kind"] = "same_population_outcome_versions"
            premise["interpretation"] = (
                "两份报告基于同一份冻结清单（同一人群、同一分组），"
                "差异只能来自结局版本/分析配置，可并列对照；"
                "效应量仍以各报告自身区间为准")
            if a.get("result") and b.get("result"):
                ea = a["result"].get("difference")
                eb = b["result"].get("difference")
                if ea and eb:
                    premise["side_by_side_effects"] = {
                        "a_estimate": ea["estimate"],
                        "a_ci": [ea["ci_low"], ea["ci_high"]],
                        "b_estimate": eb["estimate"],
                        "b_ci": [eb["ci_low"], eb["ci_high"]],
                    }
        else:
            premise["comparison_kind"] = "different_populations"
            premise["interpretation"] = (
                "两份报告绑定不同冻结清单（不同人群）。禁止把两组指标"
                "直接相减并解释为处理效应——人群构成、样本量、退出时点"
                "均可能不同。此处仅并列展示比较前提；如需效应估计，"
                "应在同一份冻结人群内重跑分析")
            premise["blocked_action"] = (
                "subtract_point_estimates_across_populations")
        return premise

    # ------------------------------------------------------------------
    def _write_meta(self, report_id: str, meta: dict) -> None:
        path = self._meta_path(report_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
