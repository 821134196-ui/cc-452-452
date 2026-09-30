"""HTTP 服务：静态前端 + JSON API，含进程内等待超时扫描任务。

一条本地启动命令：python3 run.py
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters import GateAdapter, KeyCabinetAdapter, PhotoStore
from .db import connect
from .queries import snapshot
from .seed import ensure_seed, reseed
from .service import ServiceError, YardService
from .settings import DB_PATH, PHOTO_DIR, TICK_S

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"

# 操作参数 → 服务方法
ACTION_MAP = {
    "check_in":            ("check_in", ["plate"], ["gate_lane"]),
    "assign_stall":        ("assign_stall", ["cycle", "stall"], []),
    "unhitch":             ("unhitch", ["cycle"], []),
    "intake_check":        ("intake_check", ["cycle", "seal"], ["damages"]),
    "chock":               ("chock", ["cycle"], []),
    "store_keys":          ("store_keys", ["cycle", "token", "cell"], []),
    "start_pickup":        ("start_pickup", ["cycle", "tractor", "driver"], []),
    "verify_pickup":       ("verify_pickup", ["cycle", "driver", "seal"], ["damages"]),
    "retrieve_keys":       ("retrieve_keys", ["cycle", "token", "cell"], []),
    "hitch":               ("hitch", ["cycle"], []),
    "cannot_pick":         ("cannot_pick", ["cycle", "reason"], []),
    "request_abnormal_exit": ("request_abnormal_exit", ["cycle", "note"], []),
    "decide_exception":    ("decide_exception", ["exception_id", "approve"], ["note"]),
    "exit_confirm":        ("exit_confirm", ["cycle"], []),
}


class AppContext:
    def __init__(self, fresh: bool = False):
        if fresh:
            self.conn = reseed()
        else:
            self.conn = ensure_seed()
        self.photos = PhotoStore(PHOTO_DIR)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def service(self) -> YardService:
        return YardService(self.conn, self.photos, GateAdapter(), KeyCabinetAdapter())

    def run_action(self, payload: dict) -> dict:
        user = payload.get("user")
        action = payload.get("action")
        if not user:
            raise ServiceError("缺少操作人 user", "bad_request")
        if action not in ACTION_MAP:
            raise ServiceError(f"未知操作：{action}", "bad_request")
        method_name, required, optional = ACTION_MAP[action]
        for f in required:
            if payload.get(f) is None:
                raise ServiceError(f"缺少参数：{f}", "bad_request")

        kwargs = {}
        if action == "check_in":
            kwargs = {"username": user, "plate": payload["plate"]}
            if payload.get("gate_lane"):
                kwargs["gate_lane"] = payload["gate_lane"]
        elif action == "assign_stall":
            kwargs = {"username": user, "cycle_code": payload["cycle"], "stall_code": payload["stall"]}
        elif action == "unhitch":
            kwargs = {"username": user, "cycle_code": payload["cycle"]}
        elif action == "intake_check":
            kwargs = {"username": user, "cycle_code": payload["cycle"],
                      "seal_code": payload["seal"], "damages": payload.get("damages") or {}}
        elif action == "chock":
            kwargs = {"username": user, "cycle_code": payload["cycle"]}
        elif action == "store_keys":
            kwargs = {"username": user, "cycle_code": payload["cycle"],
                      "key_token": payload["token"], "cell": payload["cell"]}
        elif action == "start_pickup":
            kwargs = {"username": user, "cycle_code": payload["cycle"],
                      "tractor_plate": payload["tractor"], "driver_license": payload["driver"]}
        elif action == "verify_pickup":
            kwargs = {"username": user, "cycle_code": payload["cycle"],
                      "driver_license": payload["driver"], "seal_code": payload["seal"],
                      "pickup_damages": payload.get("damages") or {}}
        elif action == "retrieve_keys":
            kwargs = {"username": user, "cycle_code": payload["cycle"],
                      "key_token": payload["token"], "cell": payload["cell"]}
        elif action == "hitch":
            kwargs = {"username": user, "cycle_code": payload["cycle"]}
        elif action == "cannot_pick":
            kwargs = {"username": user, "cycle_code": payload["cycle"], "reason": payload["reason"]}
        elif action == "request_abnormal_exit":
            kwargs = {"username": user, "cycle_code": payload["cycle"], "note": payload["note"]}
        elif action == "decide_exception":
            kwargs = {"username": user, "exception_id": int(payload["exception_id"]),
                      "approve": bool(payload["approve"]),
                      "note": payload.get("note", "")}
        elif action == "exit_confirm":
            kwargs = {"username": user, "cycle_code": payload["cycle"]}

        # 串行化写操作：两个调度员抢占在条件 UPDATE 处见胜负
        with self._lock:
            result = getattr(self.service(), method_name)(**kwargs)
        return {"ok": True, "result": result}

    def timeout_loop(self) -> None:
        while not self._stop.wait(TICK_S):
            with self._lock:
                try:
                    self.service().scan_timeouts()
                except Exception as e:  # 后台任务不应杀死线程
                    print("[timeout] error:", e)

    def shutdown(self):
        self._stop.set()


class Handler(BaseHTTPRequestHandler):
    ctx: AppContext = None  # 由 server 注入

    def log_message(self, *args):
        pass

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str):
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            with self.ctx._lock:
                return self._send_json({"ok": True, "state": snapshot(self.ctx.conn)})
        if path.startswith("/api/photos/"):
            name = Path(path.split("/")[-1]).name  # 防目录穿越
            if not name.endswith(".svg"):
                return self.send_error(403)
            return self._send_file(PHOTO_DIR / name, "image/svg+xml")
        if path == "/app.js":
            return self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        if path == "/styles.css":
            return self._send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "请求不是合法 JSON",
                                    "code": "bad_request"}, 400)
        try:
            if parsed.path == "/api/action":
                return self._send_json(self.ctx.run_action(payload))
            if parsed.path == "/api/reseed":
                with self.ctx._lock:
                    self.ctx.conn = reseed()
                return self._send_json({"ok": True})
            self.send_error(404)
        except ServiceError as e:
            return self._send_json({"ok": False, "error": str(e), "code": e.code},
                                   403 if e.code == "forbidden" else 409)
        except Exception as e:  # noqa: BLE001
            return self._send_json({"ok": False, "error": f"服务器错误：{e}",
                                    "code": "server"}, 500)


def create_server(host: str = "127.0.0.1", port: int = 8000,
                  fresh: bool = False) -> tuple[ThreadingHTTPServer, AppContext]:
    ctx = AppContext(fresh=fresh)
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    t = threading.Thread(target=ctx.timeout_loop, daemon=True, name="wait-timeout")
    t.start()
    httpd.ctx = ctx
    return httpd, ctx
