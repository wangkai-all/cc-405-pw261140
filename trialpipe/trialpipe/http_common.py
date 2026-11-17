"""三个独立 HTTP 服务的共享工具（纯标准库）。

服务独立部署：
  intake    :8101  只追加事件
  freezer   :8102  冻结/读取样本清单
  reporting :8103  报告生命周期与比较
服务之间通过文件存储与显式 HTTP 调用协作，不共享内存状态。
"""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "trialpipe/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 本地演示仪表盘需要跨端口读取三个服务
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS 预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _path(self) -> str:
        return urlparse(self.path).path

    def _guard(self, fn) -> None:
        try:
            fn()
        except (ValueError, KeyError) as exc:
            self._send_json({"error": str(exc), "error_type": exc.__class__.__name__},
                            status=400)
        except Exception as exc:  # 服务边界：业务异常 422，其余 500 带栈
            etype = exc.__class__.__name__
            if etype.endswith("Error") and etype in (
                    "LedgerError", "FreezeError", "ReportError"):
                self._send_json(
                    {"error": str(exc), "error_type": etype}, status=422)
            else:
                self._send_json({
                    "error": str(exc),
                    "error_type": etype,
                    "trace": traceback.format_exc(),
                }, status=500)

    def do_GET(self) -> None:
        self._guard(lambda: self.handle_get())

    def do_POST(self) -> None:
        self._guard(lambda: self.handle_post())


def make_server(host: str, port: int, handler_cls, store: dict):
    """工厂：把共享存储对象挂到 handler 类上。"""
    bound = type("BoundHandler", (handler_cls,), {"stores": store})
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer((host, port), bound)
    return httpd
