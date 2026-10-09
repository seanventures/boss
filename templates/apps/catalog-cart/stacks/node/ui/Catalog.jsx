// The catalog UI for the React/Vite scaffold: a searchable, paginated product list, a product
// detail view and a cart drawer. Adapt the markup to the app's design tokens; keep the
// behaviour: prices and totals are always the server's (never computed here), cart changes are
// JSON requests, and every control has an accessible name.
import { useCallback, useEffect, useState } from "react";

const money = (cents, currency) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);

async function api(path, init = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...init, headers: { "Content-Type": "application/json", ...(init.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { body });
  return body;
}

export function useCart() {
  const [cart, setCart] = useState(null);
  const refresh = useCallback(() => api("/api/cart").then(setCart), []);
  useEffect(() => { refresh(); }, [refresh]);
  const add = (productId, quantity = 1) => api("/api/cart/items", { method: "POST", body: JSON.stringify({ product_id: productId, quantity }) }).then(setCart);
  const setQuantity = (productId, quantity) => api(`/api/cart/items/${encodeURIComponent(productId)}`, { method: "PATCH", body: JSON.stringify({ quantity }) }).then(setCart);
  const remove = (productId) => api(`/api/cart/items/${encodeURIComponent(productId)}`, { method: "DELETE" }).then(setCart);
  return { cart, add, setQuantity, remove, refresh };
}

export function ProductList({ onOpen, onAdd }) {
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  useEffect(() => {
    const params = new URLSearchParams({ page: String(page) });
    if (q) params.set("q", q);
    api(`/api/products?${params}`).then(setData);
  }, [q, page]);
  return (
    <section aria-labelledby="catalog-title">
      <h2 id="catalog-title">Products</h2>
      <label>
        Search
        <input type="search" value={q} maxLength={100} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
      </label>
      <ul className="products">
        {(data?.items || []).map((p) => (
          <li key={p.id}>
            <button type="button" className="link" onClick={() => onOpen(p.id)}>{p.name}</button>
            <span>{money(p.price_cents, p.currency)}</span>
            <button type="button" disabled={!p.in_stock} onClick={() => onAdd(p.id)} aria-label={`Add ${p.name} to cart`}>
              {p.in_stock ? "Add" : "Out of stock"}
            </button>
          </li>
        ))}
      </ul>
      {data && data.total === 0 && <p>No products match.</p>}
      <nav aria-label="Pages">
        <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span>Page {data?.page ?? page} of {data?.pages ?? 1}</span>
        <button type="button" disabled={!data || page >= data.pages} onClick={() => setPage(page + 1)}>Next</button>
      </nav>
    </section>
  );
}

export function ProductDetail({ id, onAdd, onClose }) {
  const [p, setP] = useState(null);
  useEffect(() => { api(`/api/products/${encodeURIComponent(id)}`).then(setP).catch(() => setP(false)); }, [id]);
  if (p === false) return <p role="alert">This product is not available.</p>;
  if (!p) return null;
  return (
    <article aria-labelledby="product-title">
      <h2 id="product-title">{p.name}</h2>
      {p.image_url && <img src={p.image_url} alt={p.name} />}
      <p>{p.description}</p>
      <p>{money(p.price_cents, p.currency)}</p>
      <button type="button" disabled={!p.in_stock} onClick={() => onAdd(p.id)}>Add to cart</button>
      <button type="button" onClick={onClose}>Back</button>
    </article>
  );
}

export function CartDrawer({ cart, open, onClose, onQuantity, onRemove, onCheckout }) {
  if (!open || !cart) return null;
  return (
    <aside role="dialog" aria-modal="true" aria-labelledby="cart-title" className="drawer">
      <h2 id="cart-title">Cart</h2>
      {cart.items.length === 0 && <p>Your cart is empty.</p>}
      <ul>
        {cart.items.map((i) => (
          <li key={i.product_id}>
            <span>{i.name}</span>
            <label>
              Quantity
              <input type="number" min={0} max={99} value={i.quantity} onChange={(e) => onQuantity(i.product_id, Number(e.target.value))} />
            </label>
            <span>{money(i.line_total_cents, cart.currency)}</span>
            {!i.available && <span role="status">No longer available in this quantity</span>}
            <button type="button" onClick={() => onRemove(i.product_id)} aria-label={`Remove ${i.name}`}>Remove</button>
          </li>
        ))}
      </ul>
      <p>Subtotal: {money(cart.subtotal_cents, cart.currency)}</p>
      {onCheckout && <button type="button" disabled={cart.items.length === 0} onClick={onCheckout}>Checkout</button>}
      <button type="button" onClick={onClose}>Close</button>
    </aside>
  );
}
