from fastapi import FastAPI

from app.api import health, tasks, web


def create_app() -> FastAPI:
    app = FastAPI(title="Personal Agent OS")
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(web.router)
    return app


app = create_app()
