"""本地 WebUI（`lecture-ai serve`）。零额外依赖，只绑 localhost。"""

from lecture_ai.web.server import (
    DEFAULT_PORT,
    AppState,
    KeyStore,
    bind_error_advice,
    is_loopback,
    make_server,
)

__all__ = [
    "DEFAULT_PORT",
    "AppState",
    "KeyStore",
    "bind_error_advice",
    "is_loopback",
    "make_server",
]
