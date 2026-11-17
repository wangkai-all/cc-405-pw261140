"""三服务统一启动入口。

用法：
  python3 -I run_services.py --service intake     # :8101
  python3 -I run_services.py --service freezer    # :8102
  python3 -I run_services.py --service reporting  # :8103
  python3 -I run_services.py --service all        # 同进程演示用三线程
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trialpipe.freezer_service import build as build_freezer  # noqa: E402
from trialpipe.intake_service import build as build_intake  # noqa: E402
from trialpipe.reporting_service import build as build_reporting  # noqa: E402

PORTS = {"intake": 8101, "freezer": 8102, "reporting": 8103}


def serve(service: str, data_dir: str, host: str) -> None:
    ledger_path = os.path.join(data_dir, "ledger", "events.jsonl")
    manifest_dir = os.path.join(data_dir, "manifests")
    report_dir = os.path.join(data_dir, "reports")
    if service == "intake":
        httpd = build_intake(host, PORTS["intake"], ledger_path)
        print(f"[intake]    分组接入服务监听 http://{host}:{PORTS['intake']}")
    elif service == "freezer":
        httpd = build_freezer(host, PORTS["freezer"],
                              ledger_path, manifest_dir)
        print(f"[freezer]   样本冻结器监听 http://{host}:{PORTS['freezer']}")
    elif service == "reporting":
        httpd = build_reporting(host, PORTS["reporting"],
                                ledger_path, manifest_dir, report_dir)
        print(f"[reporting] 统计报告服务监听 http://{host}:{PORTS['reporting']}")
    elif service == "all":
        servers = [
            build_intake(host, PORTS["intake"], ledger_path),
            build_freezer(host, PORTS["freezer"], ledger_path, manifest_dir),
            build_reporting(host, PORTS["reporting"],
                            ledger_path, manifest_dir, report_dir),
        ]
        threads = []
        for httpd in servers:
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            threads.append(t)
        print("[all] 三服务已在同进程启动：",
              ", ".join(f"{k}:{v}" for k, v in PORTS.items()))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            for httpd in servers:
                httpd.shutdown()
            return
    else:
        raise SystemExit(f"未知服务 {service!r}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="trialpipe 三服务启动器")
    parser.add_argument("--service", required=True,
                        choices=["intake", "freezer", "reporting", "all"])
    parser.add_argument("--data-dir",
                        default=os.path.join(os.path.dirname(
                            os.path.abspath(__file__)), "data"))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    serve(args.service, args.data_dir, args.host)


if __name__ == "__main__":
    main()
