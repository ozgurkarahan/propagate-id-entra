"""In-memory order store with seed data."""

from datetime import datetime

ORDERS: dict[str, dict] = {}


def _seed():
    """Populate store with sample orders."""
    seed = [
        {"id": "ORD-001", "customer_name": "Alice Johnson", "product": "Wireless Keyboard", "quantity": 2, "status": "delivered", "created_at": "2025-12-01T10:00:00Z"},
        {"id": "ORD-002", "customer_name": "Bob Smith", "product": "USB-C Hub", "quantity": 1, "status": "shipped", "created_at": "2025-12-05T14:30:00Z"},
        {"id": "ORD-003", "customer_name": "Carol Lee", "product": "Monitor Stand", "quantity": 1, "status": "pending", "created_at": "2025-12-10T09:15:00Z"},
        {"id": "ORD-004", "customer_name": "David Kim", "product": "Mechanical Keyboard", "quantity": 1, "status": "shipped", "created_at": "2025-12-12T11:45:00Z"},
        {"id": "ORD-005", "customer_name": "Eve Martinez", "product": "Webcam HD", "quantity": 3, "status": "pending", "created_at": "2025-12-15T08:20:00Z"},
        {"id": "ORD-006", "customer_name": "Frank Wilson", "product": "Laptop Sleeve", "quantity": 2, "status": "delivered", "created_at": "2025-12-18T16:00:00Z"},
        {"id": "ORD-007", "customer_name": "Grace Chen", "product": "Noise-Cancelling Headphones", "quantity": 1, "status": "shipped", "created_at": "2025-12-20T13:10:00Z"},
        {"id": "ORD-008", "customer_name": "Hank Brown", "product": "Ergonomic Mouse", "quantity": 4, "status": "pending", "created_at": "2025-12-22T10:30:00Z"},
    ]
    for order in seed:
        ORDERS[order["id"]] = order


_seed()

_counter = len(ORDERS)


def list_orders() -> list[dict]:
    return list(ORDERS.values())


def get_order(order_id: str) -> dict | None:
    return ORDERS.get(order_id)


def create_order(customer_name: str, product: str, quantity: int) -> dict:
    global _counter
    _counter += 1
    order_id = f"ORD-{_counter:03d}"
    order = {
        "id": order_id,
        "customer_name": customer_name,
        "product": product,
        "quantity": quantity,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    ORDERS[order_id] = order
    return order


def update_order(order_id: str, **fields) -> dict | None:
    order = ORDERS.get(order_id)
    if order is None:
        return None
    allowed = {"customer_name", "product", "quantity", "status"}
    for key, value in fields.items():
        if key in allowed and value is not None:
            order[key] = value
    return order


def delete_order(order_id: str) -> bool:
    return ORDERS.pop(order_id, None) is not None
