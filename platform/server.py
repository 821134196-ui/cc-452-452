"""零依赖 HTTP 服务：JSON API + 静态前端 + 外观照片目录。

启动：python -m platform.server --db data/yard.db --photos data/photos --port 8000
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db as dbm
from .adapters import CabinetAdapter, GateAdapter
from .clock import Clock
from .permissions import ServiceError
from .service import WaitMonitor, YardService
from .seed import photo_svg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class AppState:
    def __init__(self, db_path: str, photos_dir: str, fixed_time: str | None,
                 wait_monitor: bool = True):
        self.db_path = db_path
        self.photos_dir = os.path.abspath(photos_dir)
        if fixed_time:
            self.clock = Clock(datetime.strptime(fixed_time, "%Y-%m-%d %H:%M:%S"))
        else:
            self.clock = Clock()
        self.gate = GateAdapter()
        self.cabinet = CabinetAdapter()
        self.svc = YardService(db_path, self.clock, self.gate, self.cabinet)
        self._restore_cabinet()
        self.sessions: dict[str, int] = {}
        self.monitor = WaitMonitor(self.svc, interval_sec=20) if wait_monitor else None
        if self.monitor:
            self.monitor.start()

    def _restore_cabinet(self) -> None:
        """从钥匙台账重建智能柜内存态（模拟适配器重启恢复）。"""
        c = self.svc.conn
        rows = c.execute(
            "SELECT tag,box_id,cell_no FROM keys WHERE status='in_cabinet'").fetchall()
        for r in rows:
            self.cabinet.put(r["tag"], r["box_id"], r["cell_no"])


class Handler(BaseHTTPRequestHandler):
    state: AppState = None  # 由 main 注入到类

    server_version = "YardYard/1.0"

    # ------------------------------------------------------------ 基础工具
    def log_message(self, fmt, *args):  # 简洁日志
        pass

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: str, message: str, status: int = 400):
        self._json({"ok": False, "error": code, "message": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ServiceError("bad_json", "请求体不是合法 JSON", http=400)

    def _cookie(self) -> dict[str, str]:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return {k: m.value for k, m in c.items()}

    def current_staff(self):
        sid = self._cookie().get("sid")
        staff_id = self.state.sessions.get(sid) if sid else None
        if not staff_id:
            return None
        return dbm.row(self.state.svc.conn, "SELECT * FROM staff WHERE id=?",
                       (staff_id,))

    def _static(self, path: str):
        # 防目录穿越
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR + os.sep) and full != WEB_DIR:
            return self._error("forbidden", "非法路径", 403)
        if os.path.isdir(full) or rel == "":
            full = os.path.join(WEB_DIR, "index.html")
        if not os.path.isfile(full):
            return self._error("not_found", "资源不存在", 404)
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _photo(self, path: str):
        full = os.path.normpath(os.path.join(self.state.photos_dir, path))
        if not full.startswith(self.state.photos_dir + os.sep):
            return self._error("forbidden", "非法路径", 403)
        if not os.path.isfile(full):
            return self._error("not_found", "照片不存在", 404)
        with open(full, "rb") as f:
            body = f.read()
        ext = os.path.splitext(full)[1]
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "image/svg+xml"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/photos/"):
                return self._photo(path[len("/photos/"):])
            return self._static(path)
        except ServiceError as e:
            return self._error(e.code, e.message, e.http)
        except sqlite3.Error as e:
            return self._error("db_error", str(e), 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            return self._api_post(path)
        except ServiceError as e:
            return self._error(e.code, e.message, e.http)
        except sqlite3.Error as e:
            return self._error("db_error", str(e), 500)

    def _api_get(self, path: str):
        s = self.state.svc
        if path == "/api/me":
            st = self.current_staff()
            return self._json({"ok": True, "staff": dict(st) if st else None,
                               "now": self.state.clock.now().strftime("%Y-%m-%d %H:%M:%S")})
        # 登录页所需公开信息（仅岗位工号/姓名/角色）
        if path == "/api/staff":
            return self._json({"ok": True, "items": s.staff_list()})
        if path == "/api/role-matrix":
            from .permissions import PERMISSIONS
            return self._json({"ok": True, "items": {
                r: sorted(list(act)) for r, act in PERMISSIONS.items()}})
        if not self.current_staff():
            return self._error("unauthorized", "请先选择岗位登录", 401)
        routes = {
            "/api/arrivals": lambda: self._json({"ok": True, "items": s.arrivals_board()}),
            "/api/queue": lambda: self._json({"ok": True, "items": s.drop_queue()}),
            "/api/slots": lambda: self._json({"ok": True, "map": s.slot_map()}),
            "/api/hitches": lambda: self._json({"ok": True, "items": s.active_hitches()}),
            "/api/keys": lambda: self._json({"ok": True, "items": s.key_ledger()}),
            "/api/exits": lambda: self._json({"ok": True, "items": s.exit_checklist()}),
            "/api/turns": lambda: self._json({"ok": True, "items": s.turns_all()}),
            "/api/events": lambda: self._json({"ok": True, "items": s.events(300)}),
            "/api/catalog": lambda: self._json({"ok": True, **self._catalog()}),
        }
        if path in routes:
            return routes[path]()
        if path.startswith("/api/turns/"):
            turn_id = int(path.rsplit("/", 1)[-1])
            return self._json({"ok": True, "turn": s.turn_detail(turn_id)})
        return self._error("not_found", "接口不存在", 404)

    def _catalog(self) -> dict:
        c = self.state.svc.conn
        return {
            "drivers": [dict(r) for r in c.execute(
                "SELECT id,name,license_no,phone,employer FROM drivers ORDER BY id")],
            "tractors": [dict(r) for r in c.execute(
                "SELECT id,plate_no,model,status FROM tractors ORDER BY id")],
            "trailers": [dict(r) for r in c.execute(
                "SELECT id,plate_no,spec,seal_no,status FROM trailers ORDER BY id")],
        }

    def _api_post(self, path: str):
        body = self._body()
        s = self.state.svc

        if path == "/api/login":
            login = (body.get("login") or "").strip()
            st = s.staff_by_login(login)
            if not st:
                raise ServiceError("bad_login", "工号不存在", http=401)
            sid = secrets.token_hex(16)
            self.state.sessions[sid] = st["id"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")
            payload = json.dumps({"ok": True, "staff": dict(st)},
                                 ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/logout":
            sid = self._cookie().get("sid")
            self.state.sessions.pop(sid, None)
            return self._json({"ok": True})

        actor = self.current_staff()
        if not actor:
            return self._error("unauthorized", "请先选择岗位登录", 401)
        aid = actor["id"]

        def ok(res=None, status=200):
            self._json({"ok": True, "result": res or {}}, status)

        if path == "/api/actions/arrive":
            return ok(s.arrive(aid, body["appointment_code"],
                               body.get("actual_tractor_id"),
                               body.get("actual_driver_id")))
        if path == "/api/actions/assign-slot":
            return ok(s.assign_slot(aid, int(body["turn_id"]), body["slot_code"]))
        if path == "/api/actions/unhitch":
            return ok(s.unhitch(aid, int(body["turn_id"])))
        if path == "/api/actions/chock":
            return ok(s.chock(aid, int(body["turn_id"])))
        if path == "/api/actions/deposit-key":
            return ok(s.deposit_key(aid, int(body["turn_id"]),
                                    body.get("box_id", "BOX-A"),
                                    str(body.get("cell_no", ""))))
        if path == "/api/actions/start-pickup":
            return ok(s.start_pickup(aid, int(body["turn_id"]),
                                     int(body["out_tractor_id"]),
                                     int(body["out_driver_id"])))
        if path == "/api/actions/verify-seal":
            return ok(s.verify_seal(aid, int(body["turn_id"]), body.get("seal_no", "")))
        if path == "/api/actions/inspect":
            return ok(s.inspect_appearance(
                aid, int(body["turn_id"]), body["phase"], body["result"],
                body.get("damage_desc", ""), body.get("photos")))
        if path == "/api/actions/cannot-pickup":
            return ok(s.cannot_pickup(aid, int(body["turn_id"]),
                                      body.get("reason", "")))
        if path == "/api/actions/hitch":
            return ok(s.hitch(aid, int(body["turn_id"])))
        if path == "/api/actions/release-key":
            return ok(s.release_key(aid, int(body["turn_id"]),
                                    body.get("cell_no")))
        if path == "/api/actions/request-approval":
            return ok(s.request_approval(aid, int(body["turn_id"]),
                                         body["reason_code"], body.get("detail", "")))
        if path == "/api/actions/decide-approval":
            return ok(s.decide_approval(aid, int(body["approval_id"]),
                                        bool(body["approve"])))
        if path == "/api/actions/exit":
            return ok(s.exit_gate(aid, int(body["turn_id"])))
        if path == "/api/mock/photo":
            return self._mock_photo(body)
        if path == "/api/dev/advance":
            minutes = float(body.get("minutes", 1))
            self.state.clock.advance(minutes=minutes)
            hits = s.scan_wait_timeouts()
            return ok({"now": self.state.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "timeouts": hits})
        if path == "/api/dev/gate-fault":
            self.state.gate.set_fault(body.get("gate_id", "GATE-IN-01"),
                                      bool(body.get("on", True)))
            return ok()
        return self._error("not_found", "接口不存在", 404)

    def _mock_photo(self, body: dict):
        """模拟现场相机：在车辆轮廓对应方位生成一张 SVG 照片。"""
        turn_id = int(body["turn_id"])
        pos = body["pos"]
        if pos not in ("front", "rear", "left", "right"):
            raise ServiceError("bad_pos", "方位必须是 front/rear/left/right", http=400)
        damaged = bool(body.get("damaged"))
        note = body.get("note") or ("异常损伤" if damaged else "外观完好")
        c = self.state.svc.conn
        row = c.execute(
            "SELECT t.id, tl.plate_no FROM turns t JOIN trailers tl "
            "ON tl.id=t.trailer_id WHERE t.id=?", (turn_id,)).fetchone()
        if not row:
            raise ServiceError("no_turn", "周转单不存在", http=404)
        tdir = os.path.join(self.state.photos_dir, f"turn_{turn_id}")
        os.makedirs(tdir, exist_ok=True)
        rel = f"turn_{turn_id}/{pos}.svg"
        with open(os.path.join(tdir, f"{pos}.svg"), "w", encoding="utf-8") as f:
            f.write(photo_svg(pos, row["plate_no"], damaged, note))
        return self._json({"ok": True, "photo": {"pos": pos, "path": rel,
                                                 "url": f"/photos/{rel}",
                                                 "note": note}})


def main():
    ap = argparse.ArgumentParser(description="物流园甩挂与钥匙交接平台")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/yard.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--fixed-time", default="2026-09-30 08:30:00",
                    help="演示时钟，设为空则使用系统时钟")
    ap.add_argument("--no-monitor", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        from .seed import seed
        print("数据库不存在，自动加载高峰样例 ...")
        seed(args.db, args.photos, reset=True)

    state = AppState(args.db, args.photos,
                     args.fixed_time or None,
                     wait_monitor=not args.no_monitor)
    Handler.state = state
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"平台已启动：http://localhost:{args.port}  （高峰时钟 {args.fixed_time}）")
    print("岗位工号：gate01 门岗 / disp01,disp02 调度 / key01 钥匙员 / "
          "insp01 查验员 / sup01 主管")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.monitor:
            state.monitor.stop()


if __name__ == "__main__":
    main()
