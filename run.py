"""
CYBERSYN INTEL // Application Launcher
Entry point for starting the monolithic CyberSyn Intel server.
"""

import sys
import uvicorn
from core.config import get_config


def main():
    config = get_config()
    web_cfg = getattr(config, "web", {}) if hasattr(config, "web") else {}
    if not isinstance(web_cfg, dict):
        web_cfg = {}
    host = web_cfg.get("host", getattr(config, "host", "0.0.0.0"))
    port = int(web_cfg.get("port", getattr(config, "port", 8080)))
    print(f"[*] Launching Hilleye on http://{host}:{port}")
    uvicorn.run("web.app:app", host=host, port=port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
