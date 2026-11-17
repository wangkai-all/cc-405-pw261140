"""统计报告服务（reporting, :8103）。

报告生命周期、输入快照还原、逐样本解释、跨人群比较拦截。
"""

from __future__ import annotations

from .http_common import JsonHandler
from .reporting import AnalysisConfig, ReportingService


class ReportingHandler(JsonHandler):
    def _config_from(self, data: dict) -> AnalysisConfig:
        for field in ("outcome_name", "outcome_version", "outcome_type",
                      "treatment", "control"):
            if field not in data:
                raise ValueError(f"分析配置缺少 {field!r}")
        return AnalysisConfig(
            outcome_name=data["outcome_name"],
            outcome_version=int(data["outcome_version"]),
            outcome_type=data["outcome_type"],
            treatment=data["treatment"],
            control=data["control"],
            alpha=float(data.get("alpha", 0.05)),
            bootstrap=bool(data.get("bootstrap", False)),
            bootstrap_seed=int(data.get("bootstrap_seed", 20261117)),
            bootstrap_reps=int(data.get("bootstrap_reps", 2000)),
        )

    def handle_get(self) -> None:
        svc: ReportingService = self.stores["reporting"]
        path = self._path()
        if path == "/health":
            self._send_json({"service": "reporting", "status": "ok"})
        elif path == "/reports":
            self._send_json({"reports": svc.list_reports()})
        elif path.startswith("/reports/"):
            rest = path[len("/reports/"):].split("/")
            report_id = rest[0]
            if len(rest) == 1:
                self._send_json(svc.load_report(report_id))
            elif len(rest) == 2 and rest[1] == "inputs":
                self._send_json(svc.restore_input_snapshot(report_id))
            elif len(rest) == 2 and rest[1] == "explain":
                self._send_json({
                    "report_id": report_id,
                    "explanations": svc.per_subject_explanation(report_id),
                })
            else:
                self._send_json({"error": "未知路径"}, 404)
        else:
            self._send_json({"error": "未知路径"}, 404)

    def handle_post(self) -> None:
        svc: ReportingService = self.stores["reporting"]
        path = self._path()
        if path == "/reports":
            data = self._read_json()
            if "manifest_id" not in data:
                raise ValueError("缺少 manifest_id")
            config = self._config_from(data.get("config", {}))
            meta = svc.create_report(
                data["manifest_id"], config,
                created_at=data.get("created_at", "unknown"),
                simulate_interruption=bool(
                    data.get("simulate_interruption", False)),
            )
            self._send_json(meta, 201)
        elif path == "/compare":
            data = self._read_json()
            premise = svc.compare_reports(
                data["report_id_a"], data["report_id_b"])
            self._send_json(premise)
        elif path.startswith("/reports/"):
            rest = path[len("/reports/"):].split("/")
            report_id = rest[0]
            if len(rest) == 2 and rest[1] == "resume":
                data = self._read_json()
                self._send_json(svc.resume_report(
                    report_id, data.get("resumed_at", "unknown")))
            elif len(rest) == 2 and rest[1] == "approve":
                data = self._read_json()
                self._send_json(svc.approve(
                    report_id, data.get("approved_at", "unknown"),
                    data.get("approver", "statistician")))
            elif len(rest) == 2 and rest[1] == "supersede":
                data = self._read_json()
                self._send_json(svc.supersede(
                    report_id, data["new_report_id"]))
            else:
                self._send_json({"error": "未知路径"}, 404)
        else:
            self._send_json({"error": "未知路径"}, 404)


def build(host: str, port: int, ledger_path: str, manifest_dir: str,
          report_dir: str):
    from .freezer import SampleFreezer
    from .http_common import make_server
    from .ledger import AppendOnlyLedger
    ledger = AppendOnlyLedger(ledger_path)
    freezer = SampleFreezer(ledger, manifest_dir)
    reporting = ReportingService(ledger, freezer, report_dir)
    return make_server(host, port, ReportingHandler,
                       {"ledger": ledger, "freezer": freezer,
                        "reporting": reporting})
