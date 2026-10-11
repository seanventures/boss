"""catalog-cart + a payment template in ONE app, per stack (spec §3.4 "Provides").

The payment templates (`stripe-checkout`, `paypal-checkout`) price a cart through their
`payments/catalog` adapter, which calls exactly:

    node    catalog.getProduct(id)       catalog.cartLineItems(cartId, req)
    python  catalog.get_product(id)      catalog.cart_line_items(cart_id, request)
    java    Catalog.product(id)          Catalog.cartLines(cartId, request)   (an interface)

and expects `[{product_id, name, unit_amount_cents, currency, quantity}]`, or null / None /
Optional.empty() for a cart the requester does not own. `catalog-cart` must provide exactly that.
Each template's own tests passing alone proved nothing about the two together (the names did not
match), so this file lays both into one app directory, deliberately: their trees collide
(package.json, main.py, requirements.txt, pom.xml, schema.sql) and every collision has a planned
merge; anything else colliding fails the layout. A small glue file wires the payments adapter to
catalog-cart's provider, the way the briefs tell the build to. The glue does NOT map catalog-cart's
CatalogError: the payment templates' adapters do that themselves (a cart that cannot be sold now
is the payment route's 400, never a 500), and the route tests below prove it through each payment
template's real checkout route, with a payment provider that must never be called.

Without any variable: the contract's names and shapes are checked in the sources, the combined
app lays out and passes both templates' gates on its files, and (when FastAPI is importable) the
Python contract test runs in the combined app. With BOSS_TEMPLATE_REAL_RUN=1 each stack's
combined app runs BOTH templates' real test commands through the gate, plus the glue's contract
test (npm / pytest / mvn on PATH; network for dependencies).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.templates.gate import evaluate, find_reports, parse_junit, template_test_files
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

APPS = REPO_ROOT / "templates" / "apps"
CATALOG = "catalog-cart"
PAYMENTS = ("stripe-checkout", "paypal-checkout")
STACKS = ("node", "python", "java")


def _report(template_id: str):
    report = validate_tree(tree_from_dir(APPS / template_id), source="builtin")
    assert report.ok, report.reasons
    return report


def _files(template_id: str) -> dict[str, bytes]:
    return tree_from_dir(APPS / template_id).files()


def _stack_files(template_id: str, stack: str) -> dict[str, bytes]:
    prefix = f"stacks/{stack}/"
    return {p[len(prefix):]: b for p, b in _files(template_id).items() if p.startswith(prefix)}


def _text(template_id: str, stack: str, rel: str) -> str:
    return (APPS / template_id / "stacks" / stack / rel).read_text()


CASES = [(p, s) for p in PAYMENTS for s in STACKS
         if (APPS / p / "boss-template.yaml").exists()
         and s in _report(p).manifest.stacks and s in _report(CATALOG).manifest.stacks]


# ---- the contract, in the sources ----------------------------------------------------------------

PAYMENTS_ADAPTER = {"node": "payments/catalog.js", "python": "payments/catalog.py",
                    "java": "src/main/java/com/example/payments/Catalog.java"}
JAVA_PROVIDER = "src/main/java/com/example/catalog/cart/CheckoutCatalog.java"


def test_both_payment_templates_share_one_catalog_adapter():
    """The glue below is shared by stripe and paypal: their adapters must be the same file."""
    for stack, rel in PAYMENTS_ADAPTER.items():
        assert _text("stripe-checkout", stack, rel) == _text("paypal-checkout", stack, rel), stack


def test_the_payments_adapter_calls_the_spec_names():
    node = _text("stripe-checkout", "node", PAYMENTS_ADAPTER["node"])
    assert "catalog.cartLineItems(String(body.cart_id), req)" in node
    assert "catalog.getProduct(" in node
    py = _text("stripe-checkout", "python", PAYMENTS_ADAPTER["python"])
    assert 'catalog.cart_line_items(str(body["cart_id"]), request)' in py
    assert "catalog.get_product(" in py
    java = _text("stripe-checkout", "java", PAYMENTS_ADAPTER["java"])
    assert "Optional<List<Line>> cartLines(String cartId, HttpServletRequest request);" in java
    assert "Optional<Product> product(String id);" in java


def test_catalog_cart_provides_exactly_those_names():
    node = _text(CATALOG, "node", "catalog/routes.mjs")
    assert "function cartLineItems(cartId, req)" in node
    assert "function getProduct(id)" in node
    assert re.search(r"return \{[^}]*\bgetProduct\b[^}]*\bcartLineItems\b[^}]*\};\n\}", node), \
        "createCatalog must return getProduct and cartLineItems"
    py = _text(CATALOG, "python", "catalog/routes.py")
    assert "def cart_line_items(self, cart_id: Any, request: Request) -> Optional[list[dict]]:" in py
    assert "def get_product(self, product_id: Any) -> Optional[dict]:" in py
    java = _text(CATALOG, "java", JAVA_PROVIDER)
    assert "public Optional<List<Line>> cartLines(String cartId, HttpServletRequest request)" in java
    assert "public Optional<Product> product(String id)" in java


def _records(java_source: str) -> dict[str, list[str]]:
    return {m.group(1): [c.strip() for c in m.group(2).split(",")]
            for m in re.finditer(r"record (\w+)\(([^)]*)\)", java_source)}


def test_the_java_records_mirror_the_payments_interface_component_for_component():
    theirs = _records(_text("stripe-checkout", "java", PAYMENTS_ADAPTER["java"]))
    ours = _records(_text(CATALOG, "java", JAVA_PROVIDER))
    for name in ("Line", "Product"):
        assert ours[name] == theirs[name], name
    assert theirs["Line"] == ["String productId", "String name", "long unitAmountCents", "String currency",
                              "long quantity"]


def test_works_with_names_real_templates():
    builtins = {p.name for p in APPS.iterdir() if (p / "boss-template.yaml").exists()}
    for name in sorted(builtins):
        for other in _report(name).manifest.works_with:
            assert other in builtins, f"{name}: works_with names {other!r}, which is not a template"
    assert {"stripe-checkout", "paypal-checkout"} <= set(_report(CATALOG).manifest.works_with)


def test_no_node_template_sets_a_package_wide_module_type():
    """A template's package.json is MERGED into the app's: a "type" there would flip every other
    template's .js between CommonJS and ESM (stripe-checkout's server.js is CommonJS). Module
    type is said per file instead (.mjs / .cjs)."""
    for pkg in sorted(APPS.glob("*/stacks/node/package.json")):
        assert "type" not in json.loads(pkg.read_text()), pkg.relative_to(APPS)


# ---- laying both templates into one app ----------------------------------------------------------

# Every path both trees hold, per stack, and the deliberate merge for it. Anything else colliding
# fails the layout.
EXPECTED_COLLISIONS = {"node": {"package.json"}, "python": {"main.py", "requirements.txt"},
                       "java": {"pom.xml", "schema.sql"}}


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _merge_package_json(catalog: bytes, payments: bytes) -> bytes:
    a, b = json.loads(catalog), json.loads(payments)
    scripts = dict(a.get("scripts", {}))
    for k, v in b.get("scripts", {}).items():
        if k in scripts and scripts[k] != v and k != "start":
            raise AssertionError(f"package.json script {k!r} differs between the templates")
        scripts.setdefault(k, v)
    scripts["start"] = "node shop.mjs"  # both start their own example server; the shop is the app
    scripts["test:compose"] = ("node --test --test-reporter=junit --test-reporter-destination=boss-compose-junit.xml "
                               "--test-reporter=spec --test-reporter-destination=stdout "
                               "tests/compose/contract.test.mjs tests/compose/route.test.mjs")
    deps = dict(a.get("dependencies", {}))
    for k, v in b.get("dependencies", {}).items():
        if k in deps and _version(deps[k]) != _version(v):
            raise AssertionError(f"package.json dependency {k}: {deps[k]} vs {v}")
        deps.setdefault(k, v)  # the same version: catalog-cart's exact pin satisfies the ^range
    out = {"name": "shop", "private": True, "version": "1.0.0", "engines": {"node": ">=20"},
           "scripts": scripts, "dependencies": deps}
    for side in (a, b):
        assert "type" not in side, "a template must not set a package-wide module type"
    return (json.dumps(out, indent=2) + "\n").encode()


_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(.*?)\s*$")


def _merge_requirements(catalog: bytes, payments: bytes) -> bytes:
    order: list[str] = []
    specs: dict[str, list[str]] = {}
    for blob in (catalog, payments):
        for line in blob.decode().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = _REQ.match(line)
            assert m, line
            name, spec = m.group(1).lower(), m.group(2)
            if name not in specs:
                order.append(name)
                specs[name] = []
            specs[name] += [s for s in spec.split(",") if s and s not in specs[name]]
    return ("".join(f"{n}{','.join(specs[n])}\n" for n in order)).encode()


def _merge_schema(catalog: bytes, payments: bytes) -> bytes:
    tables = [set(re.findall(rb"CREATE TABLE IF NOT EXISTS (\w+)", s)) for s in (catalog, payments)]
    assert not (tables[0] & tables[1]), f"both templates create {tables[0] & tables[1]}"
    return catalog.rstrip() + b"\n\n" + payments


_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _merge_pom(catalog: bytes, payments: bytes) -> bytes:
    """One Spring Boot app: the newer parent and Java, every dependency once, catalog-cart's test
    dir as the test source and the payment template's (and the glue's) added beside it."""
    poms = [ET.fromstring(catalog), ET.fromstring(payments)]

    def text(root, path):
        el = root.find(path, _POM_NS)
        return el.text.strip() if el is not None and el.text else None

    parent = max((text(r, "m:parent/m:version") for r in poms), key=_version)
    props: dict[str, str] = {}
    for r in poms:
        found = r.find("m:properties", _POM_NS)
        for el in (list(found) if found is not None else []):
            key = el.tag.split("}", 1)[1]
            if key == "java.version" and key in props:
                props[key] = max(props[key], el.text.strip(), key=_version)
            else:
                props.setdefault(key, el.text.strip())
    deps: dict[tuple[str, str], dict[str, str]] = {}
    for r in poms:
        for d in r.findall("m:dependencies/m:dependency", _POM_NS):
            fields = {f: text(d, f"m:{f}") for f in ("groupId", "artifactId", "version", "scope")}
            deps.setdefault((fields["groupId"], fields["artifactId"]), fields)
    test_dirs = [text(r, "m:build/m:testSourceDirectory") for r in poms]
    assert all(test_dirs) and test_dirs[0] != test_dirs[1], test_dirs
    dep_xml = "".join(
        "    <dependency>" + "".join(f"<{k}>{v}</{k}>" for k, v in f.items() if v) + "</dependency>\n"
        for f in deps.values())
    prop_xml = "".join(f"    <{k}>{v}</{k}>\n" for k, v in props.items())
    extra_tests = "".join(f"<source>{d}</source>" for d in (test_dirs[1], "tests/compose"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- catalog-cart + a payment template in one Spring Boot app (laid out by the BOSS compose test). -->
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{parent}</version>
    <relativePath/>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>shop</artifactId>
  <version>1.0.0</version>
  <properties>
{prop_xml}  </properties>
  <dependencies>
{dep_xml}  </dependencies>
  <build>
    <testSourceDirectory>{test_dirs[0]}</testSourceDirectory>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
      <plugin>
        <groupId>org.codehaus.mojo</groupId>
        <artifactId>build-helper-maven-plugin</artifactId>
        <version>3.6.0</version>
        <executions>
          <execution>
            <id>more-test-sources</id>
            <phase>generate-test-sources</phase>
            <goals><goal>add-test-source</goal></goals>
            <configuration><sources>{extra_tests}</sources></configuration>
          </execution>
        </executions>
      </plugin>
    </plugins>
  </build>
</project>
""".encode()


# ---- the glue: the payments adapter wired to catalog-cart's provider, as the briefs say ----------

NODE_SHOP_CATALOG = """\
// The payment template's `catalog`, backed by catalog-cart (spec §3.4): its names already match.
// A CatalogError (a cart that cannot be sold now) passes through: the payment template's adapter
// turns it into its CheckoutInputError (a 400) itself.
export function paymentsCatalog(catalog) {
  return {
    getProduct: (id) => catalog.getProduct(id),
    cartLineItems: (cartId, req) => catalog.cartLineItems(cartId, req),
  };
}
"""

NODE_SHOP = """\
// The combined app: catalog-cart's routes and the payment template's on one Express app, the
// payments priced from the shopper's own cart.
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

import { createCatalog, loadCatalogConfig } from "./catalog/routes.mjs";
import { paymentsCatalog } from "./shop_catalog.mjs";

const require = createRequire(import.meta.url);

export function createShop({ env = process.env, ...payments } = {}) {
  const catalog = createCatalog(loadCatalogConfig(env));
  const { createApp } = require("./server.js");
  const shop = createApp({ env, ...payments, catalog: paymentsCatalog(catalog) });
  shop.app.use(catalog.router);
  return { ...shop, catalog };
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const port = Number(process.env.PORT || 3000);
  createShop().app.listen(port, () => console.log(`listening on :${port}`));
}
"""

NODE_CONTRACT_TEST = """\
// The contract between catalog-cart and the payment template, in one app: the payment template's
// own pricing (payments/catalog.js priceLineItems) over catalog-cart's provider, over HTTP.
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import express from "express";

import { createCatalog } from "../../catalog/routes.mjs";
import { CatalogStore, loadSeed } from "../../catalog/store.mjs";
import { paymentsCatalog } from "../../shop_catalog.mjs";
import { createShop } from "../../shop.mjs";

const require = createRequire(import.meta.url);
const { priceLineItems, CheckoutInputError } = require("../../payments/catalog.js");

let server;
let origin;
let store;

before(async () => {
  store = new CatalogStore({ products: loadSeed() });
  const catalog = createCatalog({ cartSecret: "fake-cart-secret-for-the-compose-tests-only", secureCookies: false, currency: "usd" }, { store });
  const priced = paymentsCatalog(catalog);
  const app = express();
  app.use(catalog.router);
  app.post("/quote", express.json(), async (req, res) => {
    try {
      return res.json(await priceLineItems(priced, req.body, req));
    } catch (err) {
      if (err instanceof CheckoutInputError) return res.status(400).json({ error: err.message });
      throw err;
    }
  });
  server = app.listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
});

async function call(method, path, body, cookie) {
  const headers = { "Content-Type": "application/json" };
  if (cookie) headers.Cookie = cookie;
  const res = await fetch(origin + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const set = res.headers.getSetCookie().map((c) => c.split(";")[0]).find((c) => c.startsWith("cart="));
  return { status: res.status, body: await res.json(), cookie: set || cookie };
}

async function shopper(...items) {
  let cookie;
  for (const [productId, quantity] of items) {
    ({ cookie } = await call("POST", "/api/cart/items", { product_id: productId, quantity }, cookie));
  }
  const cart = await call("GET", "/api/cart", undefined, cookie);
  return { cookie, cartId: cart.body.id };
}

test("the payment template prices the shopper's own cart from catalog-cart, in its line shape", async () => {
  const s = await shopper(["p01", 2], ["p02", 1]);
  const r = await call("POST", "/quote", { cart_id: s.cartId, amount: 1, currency: "eur" }, s.cookie);
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.deepEqual(r.body.lines, [
    { product_id: "p01", name: "Canvas tote bag", unit_amount_cents: 1999, currency: "usd", quantity: 2 },
    { product_id: "p02", name: "Ceramic mug", unit_amount_cents: 1450, currency: "usd", quantity: 1 },
  ]);
  assert.equal(r.body.currency, "usd");
  assert.equal(r.body.amountCents, 2 * 1999 + 1450);
  store.setPrice("p01", 2100);
  assert.equal((await call("POST", "/quote", { cart_id: s.cartId }, s.cookie)).body.amountCents, 2 * 2100 + 1450);
});

test("another shopper's cart id is 'no such cart' to the payment template", async () => {
  const owner = await shopper(["p05", 1]);
  const thief = await shopper(["p06", 1]);
  for (const cookie of [thief.cookie, undefined]) {
    const r = await call("POST", "/quote", { cart_id: owner.cartId }, cookie);
    assert.equal(r.status, 400);
    assert.equal(r.body.error, "no such cart");
  }
});

test("a cart that cannot be sold is a 400 with catalog-cart's code, never a 500", async () => {
  const s = await shopper(["p07", 1]);
  store.setActive("p07", false);
  const r = await call("POST", "/quote", { cart_id: s.cartId }, s.cookie);
  assert.equal(r.status, 400);
  assert.equal(r.body.error, "product_unavailable");
  const empty = await shopper();
  assert.equal((await call("POST", "/quote", { cart_id: empty.cartId ?? "none" }, empty.cookie)).status, 400);
});

test("items without a cart are priced from catalog-cart's products, and the shop module loads", async () => {
  const r = await call("POST", "/quote", { items: [{ product_id: "p02", quantity: 3, price: 1 }] });
  assert.equal(r.status, 200);
  assert.equal(r.body.amountCents, 3 * 1450);
  assert.equal((await call("POST", "/quote", { items: [{ product_id: "p19", quantity: 1 }] })).body.error, "unknown product");
  assert.equal(typeof createShop, "function");
});
"""

PY_SHOP_CATALOG = '''\
"""The payment template's `catalog`, backed by catalog-cart (spec §3.4): its names already match.
A CatalogError (a cart that cannot be sold now) passes through: the payment template's adapter
turns it into its CheckoutInputError (a 400) itself."""
from __future__ import annotations

from typing import Any, Optional


class PaymentsCatalog:
    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog

    def get_product(self, product_id: Any) -> Optional[dict]:
        return self._catalog.get_product(product_id)

    def cart_line_items(self, cart_id: str, request: Any = None) -> Optional[list[dict]]:
        return self._catalog.cart_line_items(cart_id, request)
'''

PY_SHOP = '''\
"""The combined app: catalog-cart's routes and the payment template's on one FastAPI app, the
payments priced from the shopper's own cart."""
from __future__ import annotations

from typing import Any

from catalog import create_catalog, load_catalog_config
from shop_catalog import PaymentsCatalog


def create_shop(**payments: Any):
    import main as payments_main  # the payment template's create_app (its tests import it)

    catalog = create_catalog(load_catalog_config(payments.get("env")))
    app = payments_main.create_app(catalog=PaymentsCatalog(catalog), **payments)
    app.include_router(catalog.router)
    app.state.catalog = catalog
    return app


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(create_shop(), host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
'''

PY_CONFTEST = '''\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the app's root
'''

PY_CONTRACT_TEST = '''\
"""The contract between catalog-cart and the payment template, in one app: the payment template's
own pricing (payments/catalog.py price_line_items) over catalog-cart's provider, over HTTP."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from catalog import CatalogStore, create_catalog, load_seed
from catalog.routes import CatalogConfig
from payments.catalog import CheckoutInputError, price_line_items
from shop_catalog import PaymentsCatalog


@pytest.fixture
def store():
    return CatalogStore(load_seed())


@pytest.fixture
def app(store):
    catalog = create_catalog(CatalogConfig(cart_secret="fake-cart-secret-for-the-compose-tests-only",
                                           secure_cookies=True), store=store)
    priced = PaymentsCatalog(catalog)
    app = FastAPI()
    app.include_router(catalog.router)

    @app.post("/quote")
    async def quote(request: Request):
        try:
            return price_line_items(priced, await request.json(), request)
        except CheckoutInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return app


def shopper(app, *items):
    client = TestClient(app, base_url="https://shop.test")
    for product_id, quantity in items:
        assert client.post("/api/cart/items", json={"product_id": product_id, "quantity": quantity}).status_code == 201
    return client, client.get("/api/cart").json()["id"]


def test_the_payment_template_prices_the_shoppers_own_cart_in_its_line_shape(app, store):
    client, cart_id = shopper(app, ("p01", 2), ("p02", 1))
    r = client.post("/quote", json={"cart_id": cart_id, "amount": 1, "currency": "eur"})
    assert r.status_code == 200, r.text
    assert r.json()["lines"] == [
        {"product_id": "p01", "name": "Canvas tote bag", "unit_amount_cents": 1999, "currency": "usd", "quantity": 2},
        {"product_id": "p02", "name": "Ceramic mug", "unit_amount_cents": 1450, "currency": "usd", "quantity": 1},
    ]
    assert r.json()["currency"] == "usd" and r.json()["amount_cents"] == 2 * 1999 + 1450
    store.set_price("p01", 2100)
    assert client.post("/quote", json={"cart_id": cart_id}).json()["amount_cents"] == 2 * 2100 + 1450


def test_another_shoppers_cart_id_is_no_such_cart(app):
    _, owner_cart = shopper(app, ("p05", 1))
    thief, _ = shopper(app, ("p06", 1))
    for client in (thief, TestClient(app, base_url="https://shop.test")):
        r = client.post("/quote", json={"cart_id": owner_cart})
        assert r.status_code == 400 and r.json()["error"] == "no such cart"


def test_a_cart_that_cannot_be_sold_is_a_400_with_catalog_carts_code(app, store):
    client, cart_id = shopper(app, ("p07", 1))
    store.set_active("p07", False)
    r = client.post("/quote", json={"cart_id": cart_id})
    assert r.status_code == 400 and r.json()["error"] == "product_unavailable"


def test_items_without_a_cart_are_priced_from_catalog_carts_products(app):
    client = TestClient(app, base_url="https://shop.test")
    r = client.post("/quote", json={"items": [{"product_id": "p02", "quantity": 3, "price": 1}]})
    assert r.status_code == 200 and r.json()["amount_cents"] == 3 * 1450
    assert client.post("/quote", json={"items": [{"product_id": "p19", "quantity": 1}]}).json()["error"] == "unknown product"
'''

JAVA_GLUE = """\
package com.example.shop;

import com.example.catalog.cart.CheckoutCatalog;
import com.example.payments.Catalog;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Optional;

/** The payment template's Catalog, backed by catalog-cart's CheckoutCatalog (spec §3.4): the
 *  records mirror each other, so this only copies fields. A CatalogError (a cart that cannot be
 *  sold now) passes through: the payment template's Catalog.price turns it into its
 *  CheckoutInputException (a 400) itself. */
public class CartPaymentsCatalog implements Catalog {
  private final CheckoutCatalog cart;

  public CartPaymentsCatalog(CheckoutCatalog cart) {
    this.cart = cart;
  }

  @Override
  public Optional<Product> product(String id) {
    return cart.product(id).map(p -> new Product(p.id(), p.name(), p.unitAmountCents(), p.currency(), p.active()));
  }

  @Override
  public Optional<List<Line>> cartLines(String cartId, HttpServletRequest request) {
    return cart.cartLines(cartId, request).map(lines -> lines.stream()
        .map(l -> new Line(l.productId(), l.name(), l.unitAmountCents(), l.currency(), l.quantity())).toList());
  }
}
"""

JAVA_CONTRACT_TEST = """\
package com.example.shop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.example.catalog.cart.CatalogController;
import com.example.catalog.cart.CatalogStore;
import com.example.catalog.cart.CheckoutCatalog;
import com.example.payments.Catalog;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.Cookie;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/** The contract between catalog-cart and the payment template, in one app: the payment template's
 *  own pricing (Catalog.price) over catalog-cart's provider, with real cart cookies. */
class CatalogPaymentsContractTest {
  final CatalogStore store = new CatalogStore("usd");
  final CatalogController routes = new CatalogController(store, req -> null,
      "fake-cart-secret-for-the-compose-tests-only", "https://shop.test");
  final Catalog payments = new CartPaymentsCatalog(new CheckoutCatalog(store, routes));

  record Shopper(MockHttpServletRequest request, String cartId) {}

  Shopper shopper(Object... items) throws Exception {
    MockHttpServletRequest req = new MockHttpServletRequest();
    for (int i = 0; i < items.length; i += 2) {
      MockHttpServletResponse res = new MockHttpServletResponse();
      routes.add(new ObjectMapper().valueToTree(Map.of("product_id", items[i], "quantity", items[i + 1])), req, res);
      String set = res.getHeader("Set-Cookie");
      if (set != null) {
        String value = set.split(";", 2)[0].substring("cart=".length());
        req = new MockHttpServletRequest();
        req.setCookies(new Cookie("cart", value));
      }
    }
    Object id = routes.cart(req, new MockHttpServletResponse()).get("id");
    return new Shopper(req, id == null ? null : id.toString());
  }

  @Test
  void thePaymentTemplatePricesTheShoppersOwnCartInItsLineShape() throws Exception {
    Shopper s = shopper("p01", 2, "p02", 1);
    Catalog.Priced p = Catalog.price(payments, Map.of("cart_id", s.cartId(), "amount", 1), s.request());
    assertEquals(List.of(new Catalog.Line("p01", "Canvas tote bag", 1999, "usd", 2),
        new Catalog.Line("p02", "Ceramic mug", 1450, "usd", 1)), p.lines());
    assertEquals("usd", p.currency());
    assertEquals(2 * 1999 + 1450, p.amountCents());
    store.setPrice("p01", 2100);
    assertEquals(2 * 2100 + 1450, Catalog.price(payments, Map.of("cart_id", s.cartId()), s.request()).amountCents());
  }

  @Test
  void anotherShoppersCartIdIsNoSuchCart() throws Exception {
    Shopper owner = shopper("p05", 1);
    Shopper thief = shopper("p06", 1);
    for (MockHttpServletRequest req : List.of(thief.request(), new MockHttpServletRequest())) {
      Catalog.CheckoutInputException e = assertThrows(Catalog.CheckoutInputException.class,
          () -> Catalog.price(payments, Map.of("cart_id", owner.cartId()), req));
      assertEquals("no such cart", e.getMessage());
    }
  }

  @Test
  void aCartThatCannotBeSoldIsACheckoutInputErrorWithCatalogCartsCode() throws Exception {
    Shopper s = shopper("p07", 1);
    store.setActive("p07", false);
    Catalog.CheckoutInputException e = assertThrows(Catalog.CheckoutInputException.class,
        () -> Catalog.price(payments, Map.of("cart_id", s.cartId()), s.request()));
    assertEquals("product_unavailable", e.getMessage());
  }

  @Test
  void itemsWithoutACartArePricedFromCatalogCartsProducts() {
    MockHttpServletRequest req = new MockHttpServletRequest();
    assertEquals(3 * 1450, Catalog.price(payments,
        Map.of("items", List.of(Map.of("product_id", "p02", "quantity", 3, "price", 1))), req).amountCents());
    assertThrows(Catalog.CheckoutInputException.class, () -> Catalog.price(payments,
        Map.of("items", List.of(Map.of("product_id", "p19", "quantity", 1))), req));
  }
}
"""

NODE_ROUTE_TEST = """\
// The payment template's OWN checkout route over catalog-cart's provider, in one app (composed as
// shop.mjs composes them): a cart that cannot be sold now is a 400 with catalog-cart's code, never
// a 500, and the payment provider is never called. The glue does not map CatalogError: the payment
// template's adapter does. The provider client below records every call and fails it.
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { createRequire } from "node:module";

import { createCatalog } from "../../catalog/routes.mjs";
import { CatalogStore, loadSeed } from "../../catalog/store.mjs";
import { paymentsCatalog } from "../../shop_catalog.mjs";

const require = createRequire(import.meta.url);
const { createApp } = require("../../server.js");
const ROUTE = "@ROUTE@";
const hex = () => crypto.randomBytes(16).toString("hex");

const calls = [];
function provider(path = "provider") {
  return new Proxy(function () {}, {
    get: (_t, key) => (key === "then" ? undefined : provider(`${path}.${String(key)}`)),
    apply: () => {
      calls.push(path);
      throw new Error(`${path} was called`);
    },
  });
}

let server;
let origin;
let store;

before(async () => {
  store = new CatalogStore({ products: loadSeed() });
  const catalog = createCatalog({ cartSecret: "fake-cart-secret-for-the-compose-tests-only", secureCookies: false, currency: "usd" }, { store });
  const { app } = createApp({ env: @NODE_ENV@, @NODE_CLIENT@: provider(), catalog: paymentsCatalog(catalog) });
  app.use(catalog.router);
  server = app.listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
});

async function call(method, path, body, cookie) {
  const headers = { "Content-Type": "application/json" };
  if (cookie) headers.Cookie = cookie;
  const res = await fetch(origin + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const set = res.headers.getSetCookie().map((c) => c.split(";")[0]).find((c) => c.startsWith("cart="));
  const text = await res.text();
  return { status: res.status, body: text.startsWith("{") ? JSON.parse(text) : text, cookie: set || cookie };
}

async function shopper(...items) {
  let cookie;
  for (const [productId, quantity] of items) {
    ({ cookie } = await call("POST", "/api/cart/items", { product_id: productId, quantity }, cookie));
  }
  const cart = await call("GET", "/api/cart", undefined, cookie);
  return { cookie, cartId: cart.body.id };
}

test("an unsellable cart is the payment route's 400 with catalog-cart's code; the provider is never called", async () => {
  const s = await shopper(["p07", 1], ["p02", 1]);
  store.setActive("p07", false);
  const r = await call("POST", ROUTE, { cart_id: s.cartId }, s.cookie);
  assert.equal(r.status, 400, JSON.stringify(r.body));
  assert.deepEqual(r.body, { error: "product_unavailable" });
  assert.deepEqual(calls, []);
});

test("a sellable cart in the same shop does reach the provider (the recording client sees it)", async () => {
  const s = await shopper(["p02", 1]);
  const r = await call("POST", ROUTE, { cart_id: s.cartId }, s.cookie);
  assert.notEqual(r.status, 400, JSON.stringify(r.body));
  assert.ok(calls.length > 0, "the provider was not reached");
});
"""

PY_ROUTE_TEST = '''\
"""The payment template's OWN checkout route over catalog-cart's provider, in one app (composed as
shop.py composes them): a cart that cannot be sold now is a 400 with catalog-cart's code, never a
500, and the payment provider is never called. The glue does not map CatalogError: the payment
template's adapter does. The provider client below records every call and fails it."""
from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from catalog import CatalogStore, create_catalog, load_seed
from catalog.routes import CatalogConfig
from main import create_app
from shop_catalog import PaymentsCatalog

ROUTE = "@ROUTE@"


class RecordingProvider:
    def __init__(self, calls: list, path: str = "provider") -> None:
        self._calls, self._path = calls, path

    def __getattr__(self, name: str) -> "RecordingProvider":
        return RecordingProvider(self._calls, f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        self._calls.append(self._path)
        raise RuntimeError(f"{self._path} was called")


def shop():
    store = CatalogStore(load_seed())
    catalog = create_catalog(CatalogConfig(cart_secret="fake-cart-secret-for-the-compose-tests-only",
                                           secure_cookies=True), store=store)
    calls: list = []
    app = create_app(env=@PY_ENV@, client=RecordingProvider(calls), catalog=PaymentsCatalog(catalog))
    app.include_router(catalog.router)
    return app, store, calls


def shopper(app, *items):
    client = TestClient(app, base_url="https://shop.test", raise_server_exceptions=False)
    for product_id, quantity in items:
        assert client.post("/api/cart/items", json={"product_id": product_id, "quantity": quantity}).status_code == 201
    return client, client.get("/api/cart").json()["id"]


def test_an_unsellable_cart_is_the_payment_routes_400_and_the_provider_is_never_called():
    app, store, calls = shop()
    client, cart_id = shopper(app, ("p07", 1), ("p02", 1))
    store.set_active("p07", False)
    r = client.post(ROUTE, json={"cart_id": cart_id})
    assert r.status_code == 400, r.text
    assert r.json() == {"error": "product_unavailable"}
    assert calls == []


def test_a_sellable_cart_in_the_same_shop_does_reach_the_provider():
    app, _, calls = shop()
    client, cart_id = shopper(app, ("p02", 1))
    assert client.post(ROUTE, json={"cart_id": cart_id}).status_code != 400
    assert calls, "the provider was not reached"
'''

JAVA_ROUTE_TEST = """\
package com.example.shop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.catalog.cart.CatalogController;
import com.example.catalog.cart.CatalogStore;
import com.example.catalog.cart.CheckoutCatalog;
import com.example.payments.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.Cookie;
import java.security.SecureRandom;
import java.util.HexFormat;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/** The payment template's OWN checkout route over catalog-cart's provider, in one app: a cart that
 *  cannot be sold now is a 400 with catalog-cart's code, never a 500, and the payment provider is
 *  never called. The glue does not map CatalogError: the payment template's Catalog.price does.
 *  The provider's API address is unreachable, so any call to it would answer 502, not 400. */
class CatalogPaymentsRouteTest {
  static final String ROUTE = "@ROUTE@";
  static final String NOWHERE = "http://127.0.0.1:9";
  final CatalogStore store = new CatalogStore("usd");
  final CatalogController routes = new CatalogController(store, req -> null,
      "fake-cart-secret-for-the-compose-tests-only", "https://shop.test");
  final MockMvc mvc;

  static String hex() {
    byte[] b = new byte[16];
    new SecureRandom().nextBytes(b);
    return HexFormat.of().formatHex(b);
  }

  CatalogPaymentsRouteTest() {
    Catalog payments = new CartPaymentsCatalog(new CheckoutCatalog(store, routes));
@JAVA_BUILD@
    mvc = MockMvcBuilders.standaloneSetup(controller).build();
  }

  record Shopper(Cookie[] cookies, String cartId) {}

  Shopper shopper(Object... items) throws Exception {
    MockHttpServletRequest req = new MockHttpServletRequest();
    for (int i = 0; i < items.length; i += 2) {
      MockHttpServletResponse res = new MockHttpServletResponse();
      routes.add(new ObjectMapper().valueToTree(Map.of("product_id", items[i], "quantity", items[i + 1])), req, res);
      String set = res.getHeader("Set-Cookie");
      if (set != null) {
        String value = set.split(";", 2)[0].substring("cart=".length());
        req = new MockHttpServletRequest();
        req.setCookies(new Cookie("cart", value));
      }
    }
    Object id = routes.cart(req, new MockHttpServletResponse()).get("id");
    return new Shopper(req.getCookies(), id == null ? null : id.toString());
  }

  MockHttpServletResponse checkout(Shopper s) throws Exception {
    return mvc.perform(MockMvcRequestBuilders.post(ROUTE).contentType(MediaType.APPLICATION_JSON)
        .content(new ObjectMapper().writeValueAsString(Map.of("cart_id", s.cartId()))).cookie(s.cookies()))
        .andReturn().getResponse();
  }

  @Test
  void anUnsellableCartIsThePaymentRoutes400WithCatalogCartsCode() throws Exception {
    Shopper s = shopper("p07", 1, "p02", 1);
    store.setActive("p07", false);
    MockHttpServletResponse r = checkout(s);
    assertEquals(400, r.getStatus(), r.getContentAsString());
    assertEquals(Map.of("error", "product_unavailable"), new ObjectMapper().readValue(r.getContentAsString(), Map.class));
  }

  @Test
  void aSellableCartInTheSameShopDoesReachTheProvider() throws Exception {
    Shopper s = shopper("p02", 1);
    int status;
    try {
      status = checkout(s).getStatus();
    } catch (Exception e) {
      status = 500;
    }
    assertTrue(status >= 500, "the unreachable provider was not called: " + status);
  }
}
"""

# The payment template's own checkout route, its env and its provider client, per payment template.
ROUTES = {
    "stripe-checkout": {
        "route": "/api/checkout",
        "node_client": "stripe",
        "node_env": '{ STRIPE_SECRET_KEY: "sk_" + "test_" + hex(), STRIPE_PUBLISHABLE_KEY: "pk_" + "test_" + hex(), '
                    'STRIPE_WEBHOOK_SECRET: "whsec_" + hex(), APP_BASE_URL: "http://shop.test" }',
        "py_env": '{"STRIPE_SECRET_KEY": "sk_" + "test_" + secrets.token_hex(16), '
                  '"STRIPE_PUBLISHABLE_KEY": "pk_" + "test_" + secrets.token_hex(16), '
                  '"STRIPE_WEBHOOK_SECRET": "whsec_" + secrets.token_hex(16), "APP_BASE_URL": "https://shop.test"}',
        "java_build": """\
    StripeSettings settings = StripeSettings.load(Map.of("STRIPE_SECRET_KEY", "sk_" + "test_" + hex(),
        "STRIPE_PUBLISHABLE_KEY", "pk_" + "test_" + hex(), "STRIPE_WEBHOOK_SECRET", "whsec_" + hex(),
        "APP_BASE_URL", "https://shop.test")::get);
    com.stripe.StripeClient client = com.stripe.StripeClient.builder().setApiKey(settings.secretKey)
        .setApiBase(NOWHERE).setMaxNetworkRetries(0).build();
    StripeCheckoutController controller = new StripeCheckoutController(client, settings, new OrderStore(), payments,
        request -> false, order -> {});""",
    },
    "paypal-checkout": {
        "route": "/api/paypal/orders",
        "node_client": "paypal",
        "node_env": '{ PAYPAL_CLIENT_ID: "fake-client-" + hex(), PAYPAL_CLIENT_SECRET: "fake-" + hex(), '
                    'PAYPAL_WEBHOOK_ID: "WH" + hex(), APP_BASE_URL: "http://shop.test" }',
        "py_env": '{"PAYPAL_CLIENT_ID": "fake-client-" + secrets.token_hex(16), '
                  '"PAYPAL_CLIENT_SECRET": "fake-" + secrets.token_hex(16), '
                  '"PAYPAL_WEBHOOK_ID": "WH" + secrets.token_hex(8), "APP_BASE_URL": "https://shop.test"}',
        "java_build": """\
    PayPalSettings settings = PayPalSettings.load(Map.of("PAYPAL_CLIENT_ID", "fake-client-" + hex(),
        "PAYPAL_CLIENT_SECRET", "fake-" + hex(), "PAYPAL_WEBHOOK_ID", "WH" + hex(),
        "APP_BASE_URL", "https://shop.test")::get);
    PayPalClient client = new PayPalClient(NOWHERE, "fake-client-" + hex(), "fake-" + hex());
    PayPalCheckoutController controller = new PayPalCheckoutController(client, settings, new OrderStore(), payments,
        request -> false, order -> {});""",
    },
}


def _fill(text: str, payments_id: str) -> str:
    for key, value in ROUTES[payments_id].items():
        text = text.replace(f"@{key.upper()}@", value)
    assert not re.search(r"@[A-Z_]+@", text), "an unfilled placeholder"
    return text


GLUE = {
    "node": {"shop_catalog.mjs": NODE_SHOP_CATALOG, "shop.mjs": NODE_SHOP,
             "tests/compose/contract.test.mjs": NODE_CONTRACT_TEST},
    "python": {"shop_catalog.py": PY_SHOP_CATALOG, "shop.py": PY_SHOP, "tests/compose/conftest.py": PY_CONFTEST,
               "tests/compose/test_compose_contract.py": PY_CONTRACT_TEST},
    "java": {"src/main/java/com/example/shop/CartPaymentsCatalog.java": JAVA_GLUE,
             "tests/compose/com/example/shop/CatalogPaymentsContractTest.java": JAVA_CONTRACT_TEST},
}

ROUTE_GLUE = {
    "node": {"tests/compose/route.test.mjs": NODE_ROUTE_TEST},
    "python": {"tests/compose/test_compose_route.py": PY_ROUTE_TEST},
    "java": {"tests/compose/com/example/shop/CatalogPaymentsRouteTest.java": JAVA_ROUTE_TEST},
}

# 4 contract tests + 2 route tests per stack.
COMPOSE_TESTS = {"node": ("npm run test:compose", 6),
                 "python": ("python3 -m pytest -q tests/compose --junitxml=boss-compose-junit.xml", 6),
                 "java": ("mvn -q -B test -Dtest=CatalogPayments*", 6)}


def compose(payments_id: str, stack: str) -> dict[str, bytes]:
    """catalog-cart + `payments_id` as ONE app's files for `stack`, plus the glue."""
    cat, pay = _stack_files(CATALOG, stack), _stack_files(payments_id, stack)
    collisions = set(cat) & set(pay)
    assert collisions == EXPECTED_COLLISIONS[stack], f"{stack}: unplanned collisions {sorted(collisions)}"
    app = {**cat, **pay}
    if stack == "node":
        app["package.json"] = _merge_package_json(cat["package.json"], pay["package.json"])
    elif stack == "python":
        app["requirements.txt"] = _merge_requirements(cat["requirements.txt"], pay["requirements.txt"])
        app["main.py"] = pay["main.py"]  # the payment template's tests import create_app from it;
        # catalog-cart's tests build their own app, and shop.py is the combined entry point
    else:
        app["pom.xml"] = _merge_pom(cat["pom.xml"], pay["pom.xml"])
        app["schema.sql"] = _merge_schema(cat["schema.sql"], pay["schema.sql"])
    for rel, text in {**GLUE[stack], **ROUTE_GLUE[stack]}.items():
        assert rel not in app, rel
        app[rel] = _fill(text, payments_id).encode()
    return app


def _write(files: dict[str, bytes], root: Path) -> Path:
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return root


def _template(template_id: str, stack: str) -> dict:
    report = _report(template_id)
    return {"id": f"tpl_{template_id}", "digest": report.digest, "stack": stack,
            "manifest": report.manifest.model_dump(mode="json", by_alias=True)}


@dataclass
class _Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.mark.parametrize("payments_id,stack", CASES)
def test_the_combined_app_lays_out_with_both_templates_whole(payments_id, stack):
    app = compose(payments_id, stack)
    for template_id in (CATALOG, payments_id):
        for t in template_test_files(_files(template_id), stack):
            assert f"tests/{t}" in app, f"{template_id}: tests/{t} lost in the combined app"
    if stack == "node":
        pkg = json.loads(app["package.json"])
        for template_id in (CATALOG, payments_id):
            cmd = _report(template_id).manifest.tests["node"].command
            assert cmd.startswith("npm run ") and cmd.split()[-1] in pkg["scripts"], cmd
        assert set(json.loads(_stack_files(payments_id, "node")["package.json"])["dependencies"]) <= set(pkg["dependencies"])
    elif stack == "python":
        reqs = app["requirements.txt"].decode()
        assert "fastapi>=0.110,<1,>=0.115" in reqs
        names = [line.split(">")[0].split("=")[0] for line in reqs.splitlines()]
        assert len(names) == len(set(names)), reqs
        assert b"def create_app(" in app["main.py"] and b"catalog=" in app["main.py"]
    else:
        pom = app["pom.xml"].decode()
        assert "<version>3.5.16</version>" in pom and "<java.version>21</java.version>" in pom
        assert pom.count("<artifactId>spring-boot-starter-web</artifactId>") == 1
        if payments_id == "stripe-checkout":
            assert "<artifactId>stripe-java</artifactId>" in pom
        assert "<source>tests/compose</source>" in pom and "<testSourceDirectory>tests/java</testSourceDirectory>" in pom
        ET.fromstring(app["pom.xml"])


@pytest.mark.parametrize("payments_id,stack", CASES)
def test_the_combined_app_passes_both_templates_gates(payments_id, stack, tmp_path):
    """Both templates' gates on the ONE app's files: their tests are all present and nothing in
    the combined app (glue included) looks like a committed secret."""
    app = compose(payments_id, stack)
    for template_id in (CATALOG, payments_id):
        tpl = _template(template_id, stack)
        min_tests = tpl["manifest"]["tests"][stack]["min_tests"]

        def runner(command: str, cwd: Path) -> _Result:
            cases = "".join(f'<testcase name="t{i}"/>' for i in range(min_tests))
            (Path(cwd) / "boss-template-junit.xml").write_text(f"<testsuites>{cases}</testsuites>")
            return _Result()

        r = evaluate(template=tpl, files=_files(template_id), changed=app, cwd=tmp_path, run_command=runner)
        assert r.gates == {"template_tests": True, "no_committed_secrets": True}, (template_id, r.evidence)


@pytest.mark.parametrize("payments_id", [p for p, s in CASES if s == "python"])
def test_the_python_contract_runs_in_the_combined_app(payments_id, tmp_path):
    """No variable needed: the payment template's price_line_items over catalog-cart's provider
    (FastAPI only), and, when the payment template's own imports are installed (stripe-checkout
    needs the stripe SDK), its real checkout route refusing an unsellable cart with a 400."""
    for module in ("fastapi", "httpx"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} is not installed")
    app = _write(compose(payments_id, "python"), tmp_path / "app")
    route = payments_id != "stripe-checkout" or importlib.util.find_spec("stripe") is not None
    target = "tests/compose" if route else "tests/compose/test_compose_contract.py"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
                          cwd=app, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert f"{6 if route else 4} passed" in proc.stdout, proc.stdout[-2000:]


# ---- the real runs (opt-in) ----------------------------------------------------------------------

_TOOLS = {"node": "npm", "python": "python3", "java": "mvn"}


def _runner(stack: str):
    def run(command: str, cwd: Path) -> _Result:
        argv = shlex.split(command)
        if stack == "python":
            if argv[0] == "python3":
                argv[0] = sys.executable
            if sys.prefix != sys.base_prefix:  # in a virtualenv, --user is refused
                argv = [a for a in argv if a != "--user"]
            if "pytest" in argv:
                argv += ["-p", "no:cacheprovider"]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STRIPE_", "PAYPAL_"))}
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=1200)
        return _Result(proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:])
    return run


@pytest.mark.skipif(os.environ.get("BOSS_TEMPLATE_REAL_RUN") != "1",
                    reason="set BOSS_TEMPLATE_REAL_RUN=1 to run both templates' real tests in one app")
@pytest.mark.parametrize("payments_id,stack", CASES)
def test_both_templates_real_tests_and_the_contract_pass_in_one_app(payments_id, stack, tmp_path):
    if not shutil.which(_TOOLS[stack]):
        pytest.skip(f"{_TOOLS[stack]} is not installed")
    files = compose(payments_id, stack)
    app = _write(files, tmp_path / "app")
    run = _runner(stack)
    for template_id in (CATALOG, payments_id):
        tpl = _template(template_id, stack)
        r = evaluate(template=tpl, files=_files(template_id), changed=files, cwd=app, run_command=run)
        assert r.gates["template_tests"], (template_id, r.evidence["template_tests"])
        assert r.evidence["template_tests"]["passed"] >= tpl["manifest"]["tests"][stack]["min_tests"]
    command, expected = COMPOSE_TESTS[stack]
    started = time.time()
    result = run(command, app)
    counts = [parse_junit(p.read_bytes()) for p in find_reports(app, since=started)]
    counts = [c for c in counts if c is not None]
    assert result.exit_code == 0 and counts, result.stdout + result.stderr
    assert sum(c.passed for c in counts) >= expected and not any(c.failures or c.errors for c in counts), \
        result.stdout + result.stderr
