import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import AuthError
from .routers import parse as parse_router
from .routers import probe

app = FastAPI(title="Klovered — pipeline API (Python)")


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
    return response


@app.exception_handler(AuthError)
async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.include_router(probe.router)
app.include_router(parse_router.router)
