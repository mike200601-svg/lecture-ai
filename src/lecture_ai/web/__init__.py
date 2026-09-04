"""本地 WebUI（`lecture-ai serve`）。零额外依赖，只绑 localhost。"""

from lecture_ai.web.server import AppState, KeyStore, is_loopback, make_server

__all__ = ["AppState", "KeyStore", "is_loopback", "make_server"]
