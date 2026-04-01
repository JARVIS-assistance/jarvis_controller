import logging

from jarvis_contracts.models import ExecuteRequest, ExecuteResult, VerifyRequest, VerifyResult

logger = logging.getLogger("jarvis_controller.executor")


SUPPORTED_ACTIONS = {"click", "type", "scroll"}


def run_execute(req: ExecuteRequest) -> ExecuteResult:
    logger.info("execute request_id=%s action=%s target=%s", req.request_id, req.action, req.target)
    detail = f"mock {req.action} executed on {req.target}"
    output = {"target": req.target, "value": req.value, "mock": True}
    return ExecuteResult(
        request_id=req.request_id,
        success=True,
        action=req.action,
        detail=detail,
        output=output,
    )


def run_verify(req: VerifyRequest) -> VerifyResult:
    passed = req.expected == req.actual
    detail = "verification passed" if passed else "verification failed"
    return VerifyResult(request_id=req.request_id, passed=passed, detail=detail)
