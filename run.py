#!/usr/bin/env python3
"""一条本地启动命令：python3 run.py

选项：
  --reseed   清空并重新加载高峰样例
  --port     端口（默认 8000）
  --host     监听地址（默认 127.0.0.1）
"""
from __future__ import annotations

import argparse
import webbrowser

from app.server import create_server


def main() -> None:
    ap = argparse.ArgumentParser(description="物流园挂车甩挂与车钥匙交接平台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reseed", action="store_true", help="重新加载高峰样例")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    httpd, _ctx = create_server(args.host, args.port, fresh=args.reseed)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 56)
    print(" 物流园挂车甩挂与车钥匙交接平台 已启动")
    print(f" 打开浏览器访问：{url}")
    print(" 角色账号：zhang 门岗 / li,wang 调度 / zhou 钥匙员 /")
    print("           wu 查验员 / zhao 值班主管")
    print(" Ctrl+C 退出")
    print("=" * 56)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        _ctx.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
