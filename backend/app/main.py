from fastapi import FastAPI

from app.api import graph, health, tasks, web
from app.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Personal Agent OS")
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(graph.router)
    web.mount_web(app, get_settings())
    # 나머지 경로를 모두 받으므로 맨 뒤에 둔다.
    app.include_router(web.router)
    return app


app = create_app()
