"""FastAPI app: routes and startup (spec section 12).

Run with `python -m server.app` or `uvicorn server.app:app`.
"""

import hmac
import sys
import threading
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from server import db
from server.config import Config, ConfigError, load_config
from server.ingest import IngestRequest, ingest


def create_app(config: Config) -> FastAPI:
    db_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.connect(config.server.db_path)
        yield
        with db_lock:
            app.state.db.close()

    app = FastAPI(title="PIP Weather Station", version="0.1.0", lifespan=lifespan)

    def require_token(token: str | None) -> None:
        expected = config.server.ingest_token.encode()
        if not hmac.compare_digest((token or "").encode(), expected):
            raise HTTPException(status_code=401, detail="invalid ingest token")

    # A plain function: FastAPI runs it in a worker thread, so SQLite does not
    # block the event loop.
    @app.post("/api/ingest")
    def post_ingest(
        request: IngestRequest,
        x_ingest_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, int]:
        require_token(x_ingest_token)
        with db_lock:
            return ingest(app.state.db, config, request)

    return app


def _load_or_exit() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")


def __getattr__(name: str):
    # Built on first access so that importing this module (as tests do) does not
    # read the user's config.toml.
    if name == "app":
        app = create_app(_load_or_exit())
        globals()["app"] = app
        return app
    raise AttributeError(name)


def main() -> None:
    import uvicorn

    config = _load_or_exit()
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
