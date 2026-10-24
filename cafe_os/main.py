"""
FastAPI application for Cafe OS Intelligence Agent POS endpoints.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field

import cafe_os.db as db
from cafe_os import kds_bus
from cafe_os.db import db_lifespan, get_connection
from cafe_os.graph import run_order, set_checkpointer
from cafe_os.tools import (
    delete_order,
    get_branch_comparison,
    get_branches,
    get_branch_sales,
    get_daily_sales_report,
    get_forecast,
    get_kds_orders,
    get_menu_engineering,
    get_shifts,
    get_waste_analytics,
    open_shift,
    record_payment,
    record_waste,
    reconcile_shift,
    update_kds_status,
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with db_lifespan(app):
        # Persist LangGraph order state to SQLite so it survives restarts.
        async with AsyncSqliteSaver.from_conn_string(db.DB_PATH) as checkpointer:
            await checkpointer.setup()
            set_checkpointer(checkpointer)
            app.state.checkpointer = checkpointer
            try:
                yield
            finally:
                set_checkpointer(None)


app = FastAPI(
    title="Cafe OS Intelligence Agent",
    description="POS intelligence layer — MVP",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# API key authentication
#
# Disabled when API_KEY is unset (local development). When set, every
# /api/v1 route and the KDS WebSocket require the key, supplied either as
# the X-API-Key header or as an ?api_key= query parameter (browsers cannot
# set headers on WebSocket handshakes).
# ---------------------------------------------------------------------------

API_KEY_HEADER = "X-API-Key"


def _configured_api_key() -> str:
    return (os.environ.get("API_KEY") or "").strip()


def _key_matches(provided: Optional[str]) -> bool:
    expected = _configured_api_key()
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _extract_key(request: Request) -> Optional[str]:
    return request.headers.get(API_KEY_HEADER) or request.query_params.get("api_key")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Reject unauthenticated calls to the data API when API_KEY is set."""
    if request.url.path.startswith("/api/v1") and not _key_matches(_extract_key(request)):
        return JSONResponse(
            status_code=401,
            content={"detail": f"Missing or invalid API key — send it as the {API_KEY_HEADER} header"},
        )
    return await call_next(request)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the web dashboard."""
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "dashboard.html")
    return FileResponse(path, media_type="text/html")


@app.get("/healthz")
async def healthz():
    """Liveness probe for the platform health check (no auth required)."""
    return {"status": "ok", "service": "cafe-os"}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the SaaS landing page."""
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "landing.html")
    return FileResponse(path, media_type="text/html")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OrderItemIn(BaseModel):
    menu_item_id: str = Field(..., alias="menu_item_id")
    quantity: int = Field(..., ge=1)
    modifiers: List[str] = Field(default_factory=list)
    special_instructions: str = Field(default="")


class OrderIn(BaseModel):
    counter_number: str
    table_number: Optional[str] = None
    customer_id: Optional[str] = None
    items: List[OrderItemIn]
    payment_method: Optional[str] = None
    raw_order_text: Optional[str] = None


class OrderOut(BaseModel):
    order_id: str
    counter_number: str
    items: List[dict]
    subtotal: float
    tax: float
    total: float
    payment_status: str
    kds_status: str
    inventory_alerts: List[str]
    customer: Optional[dict] = None
    loyalty_discount: Optional[float] = None
    error: Optional[str] = None


class MenuItemOut(BaseModel):
    id: str
    name: str
    category: str
    selling_price: float
    packaging_cost: float
    active: bool


class LowStockOut(BaseModel):
    id: str
    name: str
    unit: str
    current_stock: float
    reorder_threshold: float


class CustomerOut(BaseModel):
    id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    reward_points: Optional[int] = None
    preferences: Optional[str] = None


class ReconcileShiftIn(BaseModel):
    actual_cash: float = Field(..., ge=0)


class ReconcileShiftOut(BaseModel):
    shift_id: str
    expected_cash: float
    actual_cash: float
    cash_difference: float
    flagged_for_review: bool
    status: str


class OpenShiftIn(BaseModel):
    cashier_id: str
    opening_cash: float = Field(default=0, ge=0)


class ShiftOut(BaseModel):
    id: str
    cashier_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    expected_cash: float = 0
    actual_cash: Optional[float] = None
    cash_difference: Optional[float] = None
    status: str


class DailySalesOut(BaseModel):
    date: str
    gross_revenue: float
    net_revenue: float
    tax_collected: float
    total_orders: int
    avg_basket_size: float
    cash_revenue: float
    card_revenue: float
    mobile_revenue: float
    top_item_id: Optional[str] = None
    top_item_quantity: int = 0


class MenuEngineeringOut(BaseModel):
    matrix: dict
    summary: dict


class WasteRecordIn(BaseModel):
    ingredient_id: str
    quantity: float = Field(..., ge=0)
    unit: str
    reason: str
    recorded_by: str


class WasteRecordOut(BaseModel):
    waste_id: str
    ingredient_id: str
    quantity: float
    unit: str


class WasteAnalyticsOut(BaseModel):
    waste_by_ingredient: dict
    theoretical_consumption: dict
    variance: dict
    high_variance_items: List[str]


class BranchOut(BaseModel):
    id: str
    name: str
    location: str
    manager_id: str
    status: str


class BranchSalesOut(BaseModel):
    branch_id: str
    days: int
    sales: List[dict]


class BranchComparisonOut(BaseModel):
    comparison: List[dict]


class ForecastOut(BaseModel):
    forecast: List[dict]
    method: str
    based_on_days: int
    avg_daily_revenue: float
    avg_daily_orders: float


class KdsOrderOut(BaseModel):
    kds_id: str
    order_id: str
    items: List[dict]
    status: str
    dispatched_at: str


class PayOrderIn(BaseModel):
    payment_method: str
    shift_id: Optional[str] = None


class PaymentOut(BaseModel):
    order_id: str
    payment_status: str
    payment_method: str
    total: float
    points_earned: int
    customer_reward_points: Optional[int] = None
    shift_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/orders", response_model=OrderOut)
async def create_order_endpoint(payload: OrderIn):
    """Accept a new order, invoke LangGraph, return structured response.

    A graph-level failure (e.g. unknown menu_item_id) cleans up the
    partially-created order row and returns 400 instead of a 500.
    """
    order_payload = payload.model_dump(by_alias=False)
    result = await run_order(order_payload)
    if result.get("error"):
        if result.get("order_id"):
            await delete_order(result["order_id"])
        raise HTTPException(status_code=400, detail=result["error"])
    return OrderOut(**result)


@app.post("/api/v1/demo/seed", status_code=201)
async def seed_demo_endpoint():
    """Generate demo orders with KDS tickets for dashboard display."""
    import uuid, datetime, json
    demos = [
        ("C1", "T1", "cust-001", [("latte-001", 2, ["oat-milk"], "extra shot"), ("muffin-001", 1, [], "")]),
        ("C2", "T3", None, [("cappuccino-001", 1, [], "no whip"), ("espresso-001", 2, ["extra shot"], "")]),
        ("C3", None, "cust-002", [("latte-001", 1, [], ""), ("muffin-001", 2, [], "warm")]),
        ("C1", "T2", None, [("espresso-001", 3, [], ""), ("cappuccino-001", 1, ["oat milk"], "")]),
        ("C4", "T5", None, [("muffin-001", 1, [], ""), ("latte-001", 1, ["oat-milk", "extra shot"], "decaf")]),
    ]
    created = []
    async with get_connection() as conn:
        for counter, table, cust, items in demos:
            order_id = f"ord-demo-{uuid.uuid4().hex[:8]}"
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await conn.execute(
                "INSERT INTO orders (id, counter_number, table_number, customer_id, subtotal, tax, total, payment_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, counter, table, cust, 0, 0, 0, "pending", now),
            )
            subtotal = 0.0
            items_json = []
            for menu_id, qty, mods, special in items:
                cursor = await conn.execute("SELECT name, selling_price FROM menu_items WHERE id = ?", (menu_id,))
                mrow = await cursor.fetchone()
                if not mrow:
                    continue
                unit_price = mrow["selling_price"]
                item_total = unit_price * qty
                for mod_kw in mods:
                    mcur = await conn.execute("SELECT price_delta FROM modifiers WHERE lower(keyword) = lower(?)", (mod_kw,))
                    mrow2 = await mcur.fetchone()
                    if mrow2:
                        item_total += mrow2["price_delta"] * qty
                subtotal += item_total
                item_id = f"item-{uuid.uuid4().hex[:8]}"
                await conn.execute(
                    "INSERT INTO order_items (id, order_id, menu_item_id, quantity, unit_price, modifiers_json, special_instructions) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, order_id, menu_id, qty, unit_price, json.dumps(mods), special),
                )
                items_json.append({
                    "menu_item_id": menu_id, "name": mrow["name"], "quantity": qty,
                    "modifiers": mods, "special_instructions": special,
                })
            tax = round(subtotal * 0.10, 2)
            total = round(subtotal + tax, 2)
            await conn.execute("UPDATE orders SET subtotal = ?, tax = ?, total = ? WHERE id = ?", (subtotal, tax, total, order_id))
            kds_id = f"kds-{uuid.uuid4().hex[:8]}"
            await conn.execute(
                "INSERT INTO kds_orders (id, order_id, items_json, status, dispatched_at) VALUES (?, ?, ?, ?, ?)",
                (kds_id, order_id, json.dumps(items_json), "dispatched", now),
            )
            created.append(order_id)
        await conn.commit()
    return {"created": len(created), "order_ids": created}
@app.post("/api/v1/demo/full", status_code=201)
async def full_demo_endpoint():
    """Run a fully end-to-end demo: shift, orders, KDS, payments, waste, reconciliation."""
    import uuid, datetime, json, random
    from decimal import Decimal
    from cafe_os.tools import (
        create_order, add_order_item, get_menu_item, lookup_modifiers,
        calculate_totals, dispatch_kds, deduct_inventory, record_payment,
        open_shift, reconcile_shift, record_waste, update_kds_status,
    )

    results = {"shifts": [], "orders": [], "payments": [], "kds_updates": [], "waste": []}

    # 1. Open a shift
    shift = await open_shift("cashier-demo", 200.0)
    results["shifts"].append(shift)

    # 2. Demo orders
    demos = [
        ("C1", "T1", "cust-001", [("latte-001", 2, ["oat-milk"], "extra shot"), ("muffin-001", 1, [], "")], "cash"),
        ("C2", "T3", None, [("cappuccino-001", 1, [], "no whip"), ("espresso-001", 2, ["extra shot"], "")], "card"),
        ("C3", None, "cust-002", [("latte-001", 1, [], ""), ("muffin-001", 2, [], "warm")], "mobile"),
        ("C1", "T2", None, [("espresso-001", 3, [], ""), ("cappuccino-001", 1, ["oat milk"], "")], "cash"),
        ("C4", "T5", None, [("muffin-001", 1, [], ""), ("latte-001", 1, ["oat-milk", "extra shot"], "decaf")], "card"),
        ("C2", "T4", "cust-001", [("latte-001", 1, [], "iced"), ("espresso-001", 1, [], "")], "cash"),
    ]

    kds_ids = []
    order_ids = []
    for counter, table, cust, items, pay_method in demos:
        # Create order
        order = await create_order(counter, table, cust)
        order_id = order["order_id"]
        order_ids.append(order_id)

        # Add items
        subtotal = Decimal("0")
        for menu_id, qty, mods, special in items:
            menu = await get_menu_item(menu_id)
            unit_price = Decimal(str(menu["menu_item"]["selling_price"]))
            merged = list(dict.fromkeys(mods + (await (lambda t: __import__("cafe_os.tools", fromlist=["extract_modifier_keywords"]).extract_modifier_keywords(t))(special))))
            await add_order_item(order_id, menu_id, qty, unit_price, merged, special)
            item_total = unit_price * qty
            if merged:
                lookup = await lookup_modifiers(merged)
                for mod in lookup.get("matched", []):
                    item_total += mod["price_delta"] * qty
            subtotal += item_total

        # Calculate totals
        totals = await calculate_totals(subtotal=subtotal)
        async with get_connection() as conn:
            await conn.execute("UPDATE orders SET subtotal=?, tax=?, total=? WHERE id=?",
                               (totals["subtotal"], totals["tax"], totals["total"], order_id))
            await conn.commit()

        # Dispatch KDS
        kds = await dispatch_kds(order_id)
        kds_id = kds["kds_id"] if isinstance(kds, dict) else kds.get("kds_id")
        kds_ids.append(kds_id)

        # Deduct inventory
        await deduct_inventory(order_id)

        results["orders"].append({"order_id": order_id, "total": totals["total"], "kds_id": kds_id})

    # 3. Update KDS statuses (various states)
    statuses = ["acknowledged", "in_progress", "completed", "acknowledged", "in_progress", "dispatched"]
    for kds_id, status in zip(kds_ids, statuses):
        if status != "dispatched":
            await update_kds_status(kds_id, status)
            results["kds_updates"].append({"kds_id": kds_id, "status": status})

    # 4. Pay orders (first 4)
    pay_methods = ["cash", "card", "mobile", "cash"]
    for i, (order_id, method) in enumerate(zip(order_ids[:4], pay_methods)):
        try:
            pay = await record_payment(order_id, method, shift["shift_id"])
            results["payments"].append(pay)
        except Exception as e:
            results["payments"].append({"order_id": order_id, "error": str(e)})

    # 5. Record waste
    waste_items = [
        ("ing-001", 500.0, "ml", "spoilage"),
        ("ing-006", 100.0, "ml", "overproduction"),
        ("ing-005", 5.0, "pcs", "expired"),
    ]
    for ing_id, qty, unit, reason in waste_items:
        w = await record_waste(ing_id, qty, unit, reason, "demo-staff")
        results["waste"].append(w)

    # 6. Reconcile shift
    recon = await reconcile_shift(shift["shift_id"], 350.0)
    results["shifts"].append({"reconciled": recon})

    return {
        "status": "demo complete",
        "orders_created": len(order_ids),
        "payments_processed": len(results["payments"]),
        "kds_tickets": len(kds_ids),
        "waste_records": len(results["waste"]),
        "shift_reconciled": True,
        "details": results,
    }


@app.post("/api/v1/orders/{order_id}/pay", response_model=PaymentOut)
async def pay_order_endpoint(order_id: str, payload: PayOrderIn):
    """Record payment for a pending order.

    Marks the order paid, accrues loyalty points (1 pt per whole currency
    unit of total), and banks cash totals onto the open shift's
    expected_cash — the given shift_id, or the single open shift.
    """
    try:
        result = await record_payment(
            order_id,
            payment_method=payload.payment_method,
            shift_id=payload.shift_id,
        )
        return PaymentOut(**result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/orders/{order_id}", response_model=OrderOut)
async def get_order_endpoint(order_id: str):
    """Fetch order status, items, and KDS status by order_id."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        order_row = await cursor.fetchone()

    if not order_row:
        raise HTTPException(status_code=404, detail="Order not found")

    # Fetch items
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        )
        item_rows = await cursor.fetchall()

    # Fetch KDS status
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT status FROM kds_orders WHERE order_id = ? ORDER BY dispatched_at DESC LIMIT 1",
            (order_id,),
        )
        kds_row = await cursor.fetchone()

    items = []
    for row in item_rows:
        items.append(
            {
                "order_item_id": row["id"],
                "menu_item_id": row["menu_item_id"],
                "quantity": row["quantity"],
                "unit_price": row["unit_price"],
                "modifiers": json.loads(row["modifiers_json"] or "[]"),
                "special_instructions": row["special_instructions"],
            }
        )

    kds_status = kds_row["status"] if kds_row else "unknown"

    return OrderOut(
        order_id=order_row["id"],
        counter_number=order_row["counter_number"],
        items=items,
        subtotal=order_row["subtotal"],
        tax=order_row["tax"],
        total=order_row["total"],
        payment_status=order_row["payment_status"],
        kds_status=kds_status,
        inventory_alerts=[],
        customer=None,
        loyalty_discount=None,
        error=None,
    )


@app.get("/api/v1/menu", response_model=List[MenuItemOut])
async def list_menu():
    """List all active menu items."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, category, selling_price, packaging_cost, active FROM menu_items WHERE active = 1"
        )
        rows = await cursor.fetchall()

    return [
        MenuItemOut(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            selling_price=row["selling_price"],
            packaging_cost=row["packaging_cost"],
            active=bool(row["active"]),
        )
        for row in rows
    ]


@app.get("/api/v1/inventory/low-stock", response_model=List[LowStockOut])
async def low_stock():
    """List inventory items at or below reorder threshold."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, unit, current_stock, reorder_threshold FROM inventory WHERE current_stock <= reorder_threshold"
        )
        rows = await cursor.fetchall()

    return [
        LowStockOut(
            id=row["id"],
            name=row["name"],
            unit=row["unit"],
            current_stock=row["current_stock"],
            reorder_threshold=row["reorder_threshold"],
        )
        for row in rows
    ]


@app.get("/api/v1/customers/{customer_id}", response_model=Optional[CustomerOut])
async def get_customer(customer_id: str):
    """Look up customer by ID for CRM personalization (stub for MVP)."""
    try:
        async with get_connection() as db:
            cursor = await db.execute(
                "SELECT id, name, phone, reward_points, preferences FROM customers WHERE id = ?",
                (customer_id,),
            )
            row = await cursor.fetchone()

        if row:
            return CustomerOut(
                id=row["id"],
                name=row["name"],
                phone=row["phone"],
                reward_points=row["reward_points"],
                preferences=row["preferences"],
            )
        raise HTTPException(status_code=404, detail="Customer not found")
    except HTTPException:
        raise
    except Exception as exc:
        if "no such table" in str(exc).lower():
            raise HTTPException(status_code=404, detail="CRM not initialized")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/shifts", response_model=ShiftOut, status_code=201)
async def open_shift_endpoint(payload: OpenShiftIn):
    """Open a cashier shift; expected_cash starts at the opening float."""
    result = await open_shift(payload.cashier_id, payload.opening_cash)
    return ShiftOut(**result)


@app.get("/api/v1/shifts", response_model=List[ShiftOut])
async def list_shifts_endpoint(status: Optional[str] = None):
    """List shifts newest-first; pass ?status=open or ?status=closed to filter."""
    return [ShiftOut(**row) for row in await get_shifts(status)]


@app.post("/api/v1/shifts/{shift_id}/reconcile", response_model=ReconcileShiftOut)
async def reconcile_shift_endpoint(shift_id: str, payload: ReconcileShiftIn):
    """Cash drawer reconciliation: compare actual cash vs expected for a shift.

    Reconciling is what closes a shift — it records the counted drawer,
    stamps ended_at, and sets status to 'closed'.
    """
    try:
        result = await reconcile_shift(shift_id, payload.actual_cash)
        return ReconcileShiftOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/daily-sales/{date}", response_model=DailySalesOut)
async def daily_sales_report_endpoint(date: str):
    """Get daily sales report for a given date (YYYY-MM-DD)."""
    try:
        result = await get_daily_sales_report(date)
        return DailySalesOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/menu-engineering", response_model=MenuEngineeringOut)
async def menu_engineering_endpoint():
    """Get menu engineering matrix: Stars, Puzzles, Plowhorses, Dogs."""
    try:
        result = await get_menu_engineering()
        return MenuEngineeringOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/inventory/waste", response_model=WasteRecordOut)
async def record_waste_endpoint(payload: WasteRecordIn):
    """Record actual waste/spoilage for an ingredient."""
    try:
        result = await record_waste(
            ingredient_id=payload.ingredient_id,
            quantity=payload.quantity,
            unit=payload.unit,
            reason=payload.reason,
            recorded_by=payload.recorded_by,
        )
        return WasteRecordOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/waste", response_model=WasteAnalyticsOut)
async def waste_analytics_endpoint(date: Optional[str] = None):
    """Get waste analytics: actual vs theoretical consumption variance."""
    try:
        result = await get_waste_analytics(date=date)
        return WasteAnalyticsOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches", response_model=List[BranchOut])
async def list_branches():
    """List all branches."""
    try:
        result = await get_branches()
        return [BranchOut(**b) for b in result["branches"]]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches/{branch_id}/sales", response_model=BranchSalesOut)
async def branch_sales(branch_id: str, days: int = 7):
    """Get sales for a specific branch over the last N days."""
    try:
        result = await get_branch_sales(branch_id, days=days)
        return BranchSalesOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches/compare", response_model=BranchComparisonOut)
async def compare_branches():
    """Compare performance metrics across all branches."""
    try:
        result = await get_branch_comparison()
        return BranchComparisonOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/forecast", response_model=ForecastOut)
async def forecast_endpoint(days: int = 7):
    """Get sales forecast for the next N days based on historical data."""
    try:
        result = await get_forecast(days=days)
        return ForecastOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/kds/orders", response_model=List[KdsOrderOut])
async def list_kds_orders(status: Optional[str] = None, include_completed: bool = False):
    """List kitchen display tickets. Defaults to active (non-completed) ones."""
    try:
        result = await get_kds_orders(status=status, include_completed=include_completed)
        return [KdsOrderOut(**order) for order in result["orders"]]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.websocket("/ws/kds")
async def kds_websocket(websocket: WebSocket):
    """Kitchen display channel: live order pushes plus station status updates.

    On connect, sends {"type": "snapshot", "orders": [...]} with all active
    tickets. Every new dispatch pushes {"type": "order_dispatched", ...}.
    Clients send {"type": "status_update", "kds_id": ..., "status": ...} with
    status in acknowledged|in_progress|completed; updates are rebroadcast to
    all connected displays. {"type": "ping"} gets a {"type": "pong"}.
    """
    if not _key_matches(
        websocket.headers.get(API_KEY_HEADER) or websocket.query_params.get("api_key")
    ):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = kds_bus.subscribe()
    sender: Optional[asyncio.Task] = None
    try:
        backlog = await get_kds_orders()
        await websocket.send_json({"type": "snapshot", "orders": backlog["orders"]})

        async def _forward_events() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        sender = asyncio.create_task(_forward_events())

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            msg_type = message.get("type")
            if msg_type == "status_update":
                try:
                    await update_kds_status(str(message.get("kds_id")), str(message.get("status")))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if sender is not None:
            sender.cancel()
        kds_bus.unsubscribe(queue)
