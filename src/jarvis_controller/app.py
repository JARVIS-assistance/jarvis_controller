import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jarvis_contracts.models import ErrorResponse, ExecuteRequest, VerifyRequest

from .executor import SUPPORTED_ACTIONS, run_execute, run_verify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis_controller")

app = FastAPI(title="jarvis-controller", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "jarvis-controller"}


@app.post("/execute")
def execute(req: ExecuteRequest, request: Request):
    if req.action not in SUPPORTED_ACTIONS:
        err = ErrorResponse(
            error_code="UNSUPPORTED_ACTION",
            message=f"unsupported action: {req.action}",
            request_id=req.request_id,
            details={"allowed": sorted(SUPPORTED_ACTIONS)},
        )
        logger.error("execute failed request_id=%s reason=unsupported_action", req.request_id)
        return JSONResponse(status_code=400, content=err.model_dump())
    result = run_execute(req)
    logger.info("execute success request_id=%s", req.request_id)
    return result


@app.post("/verify")
def verify(req: VerifyRequest, request: Request):
    result = run_verify(req)
    logger.info("verify request_id=%s passed=%s", req.request_id, result.passed)
    return result
