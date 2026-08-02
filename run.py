#!/usr/bin/env python3
"""
Pi Orchestrator — 启动入口
用法: python run.py [--host 0.0.0.0] [--port 8020]

退出可靠性（Windows）:
  - 显式注册 SIGINT/SIGTERM → server.should_exit（Ctrl+C 立即响应）
  - timeout_graceful_shutdown=3：优雅关闭最多 3 秒，
    防止 SSE 长连接（/api/stream 浏览器常驻）导致退出无限挂起
  - 二次 Ctrl+C 强制退出（键盘中断直接抛 KeyboardInterrupt）
"""
import argparse
import signal
import sys

import uvicorn
from uvicorn import Config, Server

GRACEFUL_SHUTDOWN_SECONDS = 3


def main():
    parser = argparse.ArgumentParser(description="Pi Orchestrator Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8020, help="监听端口")
    args = parser.parse_args()

    print(f"""
    ╔══════════════════════════════════════╗
    ║   ⚙️  Pi Orchestrator               ║
    ║   轻量级 Agent 任务调度器              ║
    ║                                      ║
    ║   🌐  Web UI:  http://{args.host}:{args.port}
    ║   📡  API:     http://{args.host}:{args.port}/api
    ║   💚  Health:  http://{args.host}:{args.port}/health
    ╚══════════════════════════════════════╝
    """)

    config = Config(
        "server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = Server(config)

    def _request_shutdown(sig, frame):
        """SIGINT/SIGTERM → 请求优雅关闭（3 秒超时强制退出）"""
        print(f"\n⏹️  收到信号 {sig}，正在关闭（最长 {GRACEFUL_SHUTDOWN_SECONDS}s）...")
        server.should_exit = True

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    try:
        server.run()
    except KeyboardInterrupt:
        # 二次 Ctrl+C 或信号被终端吃掉 → 强制退出
        print("\n🛑 强制退出")
        sys.exit(130)
    finally:
        print("✅ Server 已退出，端口已释放")


if __name__ == "__main__":
    main()
