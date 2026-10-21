"""Knowledge Graph Phase 1: tree-sitter extraction per language (spec §3.3, §7 "fixtures per language").

Each fixture is a small real-shaped file; the assertions are what the code graph needs from it:
definitions with their scope, imports resolved to modules, references attributed to the right
definition, and the framework facts (endpoints, tables, queues/webhooks, config keys).
"""
from __future__ import annotations

from boss_engine.services.knowledge.code_parse import language_for, module_name, parse_file

PY = b'''
import os
from django.db import models
from ..core.utils import send as dispatch
from . import tasks
from .webhooks import WebhookEventAsyncType

RESERVE_SQL = "INSERT INTO stock_reservation (line_id) VALUES (%s)"


class Reservation(models.Model):
    class Meta:
        db_table = "stock_reservation"

    @property
    def remaining(self):
        return self.quantity

    def save(self, *args, **kwargs):
        dispatch(self)
        self.validate()


def reserve(line):
    """Reserve stock. SELECT nothing FROM this docstring."""
    key = os.environ["NORTHPEAK_WEBHOOK_SECRET"]
    tasks.notify.delay(line)
    trigger(WebhookEventAsyncType.ORDER_CREATED)
    return Reservation(line=line, key=key)


@app.post("/webhooks/saleor/order-created")
def order_created(request):
    return reserve(request.json)
'''


def test_python_definitions_imports_references_and_framework_facts():
    f = parse_file("saleor/warehouse/reservations.py", PY)
    assert f.module == "saleor.warehouse.reservations" and f.error is None
    defs = {d.qualname: d.kind for d in f.defs}
    assert defs == {"Reservation": "class", "Reservation.Meta": "class", "Reservation.remaining": "property",
                    "Reservation.save": "method", "reserve": "function", "order_created": "function"}
    imports = {(i.alias, i.module, i.name) for i in f.imports}
    assert ("dispatch", "saleor.core.utils", "send") in imports           # relative import, aliased
    assert ("tasks", "saleor.warehouse", "tasks") in imports              # "from . import x"
    assert ("os", "os", None) in imports
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("Reservation.save", ("dispatch",), True) in refs
    assert ("Reservation.save", ("self", "validate"), True) in refs
    assert ("reserve", ("Reservation",), True) in refs
    assert ("reserve", ("tasks", "notify", "#publish"), True) in refs     # Celery .delay
    assert not any(r.chain == ("Reservation",) and r.src == "" for r in f.refs)   # a def name is not a ref
    assert ("Reservation", "stock_reservation", "defines") in f.tables              # Meta.db_table wins
    assert ("", "stock_reservation", "writes") in f.tables
    assert not any(t[1] == "nothing" for t in f.tables)                             # docstrings are not SQL
    assert ("reserve", "NORTHPEAK_WEBHOOK_SECRET") in f.config
    assert ("reserve", "webhook:ORDER_CREATED", "publishes") in f.queues
    assert ("order_created", "POST", "/webhooks/saleor/order-created") in f.endpoints
    assert ("order_created", "webhook:ORDER_CREATED", "subscribes") in f.queues     # the consumer side


TS = b'''
import express from "express";
import { reserve as hold, Stock } from "./stock";
import * as db from "../db/index";

const app = express();
export const total = (items: Stock[]) => items.reduce((n, i) => n + i.qty, 0);

export class OrderService {
  place(order: Order) {
    hold(order.lines);
    return db.query("SELECT * FROM warehouse_stock WHERE id = $1");
  }
}

app.post("/orders", placeOrder);
function placeOrder(req, res) { new OrderService().place(req.body); const k = process.env.PAYMENT_KEY; }
'''


def test_typescript_es_imports_arrow_functions_classes_express_and_env():
    f = parse_file("services/orders/api.ts", TS)
    assert language_for("services/orders/api.ts") == "typescript" and f.module == "services/orders/api"
    defs = {d.qualname: d.kind for d in f.defs}
    assert defs == {"total": "function", "OrderService": "class", "OrderService.place": "method",
                    "placeOrder": "function"}
    imports = {(i.alias, i.module, i.name) for i in f.imports}
    assert ("hold", "services/orders/stock", "reserve") in imports
    assert ("db", "services/db", None) in imports                         # ../db/index -> services/db
    assert ("express", "express", "default") in imports
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("OrderService.place", ("hold",), True) in refs
    assert ("OrderService.place", ("db", "query"), True) in refs
    assert ("OrderService.place", "warehouse_stock", "reads") in f.tables
    assert ("#ref:placeOrder", "POST", "/orders") in f.endpoints
    assert ("placeOrder", "PAYMENT_KEY") in f.config


GO = b'''
package fulfilment

import (
	"net/http"
	"os"
	store "github.com/northpeak/fulfilment/internal/store"
)

type Server struct{ db *store.DB }

func (s *Server) Reserve(w http.ResponseWriter, r *http.Request) {
	store.Reserve(s.db, r.URL.Query().Get("line"))
	helper()
}

func helper() {}

func Routes(s *Server) {
	secret := os.Getenv("NORTHPEAK_WEBHOOK_SECRET")
	http.HandleFunc("/webhooks/saleor/order-created", s.Reserve)
	_ = secret
}
'''


def test_go_methods_on_receivers_package_imports_handlers_and_getenv():
    f = parse_file("internal/fulfilment/server.go", GO)
    assert f.module == "internal/fulfilment"
    defs = {d.qualname: d.kind for d in f.defs}
    assert defs == {"Server": "class", "Server.Reserve": "method", "helper": "function", "Routes": "function"}
    assert ("store", "github.com/northpeak/fulfilment/internal/store", None) in {(i.alias, i.module, i.name) for i in f.imports}
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("Server.Reserve", ("store", "Reserve"), True) in refs
    assert ("Server.Reserve", ("helper",), True) in refs
    assert ("Routes", "NORTHPEAK_WEBHOOK_SECRET") in f.config
    assert ("#ref:s.Reserve", "ANY", "/webhooks/saleor/order-created") in f.endpoints


JAVA = b'''
package com.northpeak.orders;

import com.northpeak.stock.StockService;
import org.springframework.web.bind.annotation.PostMapping;

public class OrderController {
    private final StockService stock;

    @PostMapping("/orders")
    public Order place(OrderRequest req) {
        stock.reserve(req.lines());
        return Order.from(req);
    }
}
'''


def test_java_packages_imports_invocations_and_spring_mappings():
    f = parse_file("src/main/java/com/northpeak/orders/OrderController.java", JAVA)
    assert f.module == "com.northpeak.orders"
    assert {d.qualname for d in f.defs} == {"OrderController", "OrderController.place"}
    assert ("StockService", "com.northpeak.stock", "StockService") in {(i.alias, i.module, i.name) for i in f.imports}
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("OrderController.place", ("stock", "reserve"), True) in refs
    assert ("OrderController.place", ("Order", "from"), True) in refs
    assert ("OrderController", ("StockService",), False) in refs          # a type use is a reference
    assert ("OrderController.place", "POST", "/orders") in f.endpoints


CS = b'''
using NorthPeak.Stock;

namespace NorthPeak.Orders
{
    public class OrdersController
    {
        [HttpPost("orders")]
        public IActionResult Place(OrderRequest req)
        {
            StockService.Reserve(req.Lines);
            var key = Environment.GetEnvironmentVariable("PAYMENT_KEY");
            return Ok();
        }
    }
}
'''


def test_csharp_namespaces_usings_invocations_attributes_and_env():
    f = parse_file("src/Orders/OrdersController.cs", CS)
    assert f.module == "NorthPeak.Orders"
    assert {d.qualname for d in f.defs} == {"OrdersController", "OrdersController.Place"}
    assert ("*", "NorthPeak.Stock", "*") in {(i.alias, i.module, i.name) for i in f.imports}
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("OrdersController.Place", ("StockService", "Reserve"), True) in refs
    assert ("OrdersController.Place", "POST", "orders") in f.endpoints
    assert ("OrdersController.Place", "PAYMENT_KEY") in f.config


def test_a_file_that_cannot_be_parsed_records_its_error_and_never_raises():
    big = parse_file("gen/bundle.js", b"x" * 1_100_000)
    assert big.error == "too large" and big.defs == []
    broken = parse_file("broken.py", b"def f(:\n  return (((")
    assert broken.error is None and isinstance(broken.defs, list)     # tree-sitter recovers
    assert module_name("pkg/__init__.py", "python") == "pkg"
    assert module_name("web/src/index.ts", "typescript") == "web/src"


def test_a_method_called_on_an_expression_is_recorded_for_the_unique_name_rule():
    """x.filter(...).not_expired() and Manager(...).run(): the receiver is a call, so its type is unknown."""
    f = parse_file("m.py", b"def f():\n    Reservation.objects.filter(a=1).not_expired()\n    Manager(p).calculate_total(x)\n")
    refs = {(r.src, r.chain, r.call) for r in f.refs}
    assert ("f", ("?", "not_expired"), True) in refs
    assert ("f", ("?", "calculate_total"), True) in refs
    assert ("f", ("Reservation", "objects", "filter"), True) in refs


def test_typescript_re_exports_scoped_packages_and_namespace_types():
    """`export ... from` re-exports, an npm scope's @ kept, and a type reached through a namespace."""
    src = (b'export { userLoader as loader } from "./loader"\n'
           b'export * from "./pg"\n'
           b'export * as OrderWorkflow from "./order"\n'
           b'import { HttpTypes } from "@medusajs/types"\n'
           b'export function f(m: Match<HttpTypes.AdminRole>): OrderWorkflow.Input { return m }\n'
           b'export function g() { const xs: HttpTypes.AdminPrice[] = []; return xs }\n')
    f = parse_file("pkg/src/index.ts", src)
    imports = {(i.alias, i.module, i.name) for i in f.imports}
    assert ("loader", "pkg/src/loader", "userLoader") in imports
    assert ("*", "pkg/src/pg", "*") in imports
    assert ("OrderWorkflow", "pkg/src/order", None) in imports
    assert ("HttpTypes", "@medusajs/types", "HttpTypes") in imports
    refs = {(r.src, r.chain) for r in f.refs}
    assert ("f", ("HttpTypes", "AdminRole")) in refs and ("f", ("OrderWorkflow", "Input")) in refs
    assert ("g", ("HttpTypes", "AdminPrice")) in refs           # a variable's type annotation
