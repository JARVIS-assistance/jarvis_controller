import logging

from fastapi import FastAPI

from .middleware.core_client import CoreClient
from .middleware.auth_middleware import GatewayAuthMiddleware
from .middleware.gateway_client import GatewayClient
from .router.router import api_router

logging.basicConfig(level=logging.INFO)


def create_app(
    gateway_client: GatewayClient | None = None,
    core_client: CoreClient | None = None,
) -> FastAPI:
    app = FastAPI(title="jarvis-controller", version="0.1.0")
    app.state.gateway_client = gateway_client or GatewayClient()
    app.state.core_client = core_client or CoreClient()
    app.add_middleware(GatewayAuthMiddleware, gateway_client=app.state.gateway_client)
    app.include_router(api_router)
    return app


app = create_app()
