"""
CYBERSYN INTEL // Application Launcher
Entry point for starting the monolithic CyberSyn Intel server.
"""

import sys
import uvicorn
from core.config import get_config


def main():
    config = get_config()
    host = getattr(config, "host", "0.0.0.0")
    port = int(getattr(config, "port", 8000))
    print(f"[*] Launching CyberSyn Intel on http://{host}:{port}")
    uvicorn.run("web.app:app", host=host, port=port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
