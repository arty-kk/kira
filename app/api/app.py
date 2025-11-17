#app/api/app.py
from fastapi import FastAPI

from app.api.conversation import router as conversation_router


def create_app() -> FastAPI:
    app = FastAPI(title="Conversation API", docs_url=None, redoc_url=None)

    app.include_router(conversation_router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app