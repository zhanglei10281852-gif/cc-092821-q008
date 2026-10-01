from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import audit, auth, roles, system, users
from app.core.errors import DomainError
from app.database import close_connection, init_db, transaction
from app.germplasm.router import router as germplasm_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    # 服务重启后回收所有已到期但尚未进入拣货的库存预约，释放锁定重量。
    from app.germplasm.service import GermplasmService

    with transaction(immediate=True) as connection:
        GermplasmService(connection).reservations.expire_due()
    yield
    close_connection()


app = FastAPI(title="种质资源入库与活力复检服务", version="1.0.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "context": exc.context}},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(germplasm_router)


@app.get("/")
def root() -> dict:
    return {"service": "种质资源入库与活力复检服务", "version": "1.0.0"}
