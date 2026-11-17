"""样本冻结器服务（freezer, :8102）。

读取接入服务的台账（同一文件存储，读前强制哈希验真），
按 as-of 标准产出不可变清单并提供清单查询。
"""

from __future__ import annotations

from .freezer import FreezeCriteria, SampleFreezer
from .http_common import JsonHandler


class FreezerHandler(JsonHandler):
    def handle_get(self) -> None:
        freezer: SampleFreezer = self.stores["freezer"]
        path = self._path()
        if path == "/health":
            self._send_json({"service": "freezer", "status": "ok"})
        elif path == "/manifests":
            self._send_json({"manifests": freezer.list_manifests()})
        elif path.startswith("/manifests/"):
            manifest_id = path.split("/", 2)[2]
            manifest = freezer.load(manifest_id)
            self._send_json(manifest)
        else:
            self._send_json({"error": "未知路径"}, 404)

    def handle_post(self) -> None:
        freezer: SampleFreezer = self.stores["freezer"]
        path = self._path()
        if path == "/freeze":
            data = self._read_json()
            for field in ("as_of", "study_id", "groups"):
                if field not in data:
                    raise ValueError(f"缺少冻结参数 {field!r}")
            criteria = FreezeCriteria(
                as_of=data["as_of"],
                study_id=data["study_id"],
                groups=tuple(data["groups"]),
                exclude_withdrawn_by_as_of=bool(
                    data.get("exclude_withdrawn_by_as_of", True)),
                require_assignment=bool(data.get("require_assignment", True)),
            )
            manifest = freezer.freeze(criteria, data.get("manifest_id"))
            self._send_json(manifest, 201)
        else:
            self._send_json({"error": "未知路径"}, 404)


def build(host: str, port: int, ledger_path: str, manifest_dir: str):
    from .http_common import make_server
    from .ledger import AppendOnlyLedger
    ledger = AppendOnlyLedger(ledger_path)
    freezer = SampleFreezer(ledger, manifest_dir)
    return make_server(host, port, FreezerHandler,
                       {"ledger": ledger, "freezer": freezer})
