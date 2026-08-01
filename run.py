#!/usr/bin/env python3
"""
Pi Orchestrator — 启动入口
用法: python run.py [--host 0.0.0.0] [--port 8020]
"""
import argparse
import uvicorn

if __name__ == "__main__":
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

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=False
    )
