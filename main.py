"""
Entry point for local development.

Run with:
    python main.py

Optional env overrides:
    PORT=8080 python main.py
    RELOAD=false python main.py
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    reload = os.getenv("RELOAD", "true").lower() != "false"
    host = os.getenv("HOST", "0.0.0.0")

    print(f"\n  social-mediamgr-service")
    print(f"  Running on http://{host}:{port}")
    print(f"  Docs:    http://{host}:{port}/docs")
    print(f"  Health:  http://{host}:{port}/health")
    print(f"  Reload:  {reload}\n")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
