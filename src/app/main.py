from fastapi import FastAPI

from app.api.routes import router

app = FastAPI(title="Autonomous Operations Agent")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
