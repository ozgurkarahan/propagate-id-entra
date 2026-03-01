"""Orders API — FastAPI CRUD service."""

import base64
import json
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from data import list_orders, get_order, create_order, update_order, delete_order

# --- Azure Monitor OpenTelemetry ---
_conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _conn_str:
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(connection_string=_conn_str)
    # OTel adds handler at WARNING level; lower to INFO for app logs.
    # Add StreamHandler so logs also appear in container logs.
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _root.addHandler(_h)
    # Suppress verbose Azure SDK HTTP logging
    logging.getLogger("azure").setLevel(logging.WARNING)
    print("Azure Monitor OpenTelemetry configured for orders-api")
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

logger = logging.getLogger("orders-api")

app = FastAPI(title="Orders API", version="1.0.0")

# Explicit instrumentation — auto-discovery may fail with vendored deps (pip --target)
if _conn_str:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pass


@app.middleware("http")
async def log_token_claims(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "none")
    mcp_session = request.headers.get("Mcp-Session-Id", "none")
    logger.info(
        "request_start method=%s path=%s request_id=%s mcp_session=%s",
        request.method, request.url.path, request_id, mcp_session,
    )

    auth_header = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.split(" ", 1)
        token_type = parts[0] if parts else "unknown"
        logger.info("Authorization header present — type: %s", token_type)
        if token_type.lower() == "bearer" and len(parts) == 2:
            try:
                # JWT has 3 dot-separated parts; the payload is the second
                payload_b64 = parts[1].split(".")[1]
                # Fix base64 padding
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                claims = {k: payload.get(k) for k in ("sub", "aud", "iss", "exp", "name")}
                logger.info("Token claims: %s", json.dumps(claims, default=str))
            except Exception:
                logger.warning("Failed to decode JWT payload")
    else:
        logger.info("No Authorization header on %s %s", request.method, request.url.path)

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


class CreateOrderRequest(BaseModel):
    customer_name: str
    product: str
    quantity: int


class UpdateOrderRequest(BaseModel):
    customer_name: str | None = None
    product: str | None = None
    quantity: int | None = None
    status: str | None = None


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/orders")
def get_orders():
    return list_orders()


@app.get("/orders/{order_id}")
def get_order_by_id(order_id: str):
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return order


@app.post("/orders", status_code=201)
def create_new_order(body: CreateOrderRequest):
    return create_order(
        customer_name=body.customer_name,
        product=body.product,
        quantity=body.quantity,
    )


@app.put("/orders/{order_id}")
def update_existing_order(order_id: str, body: UpdateOrderRequest):
    updated = update_order(
        order_id,
        customer_name=body.customer_name,
        product=body.product,
        quantity=body.quantity,
        status=body.status,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return updated


@app.delete("/orders/{order_id}", status_code=204)
def delete_existing_order(order_id: str):
    if not delete_order(order_id):
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
