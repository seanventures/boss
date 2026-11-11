// The tickets, kept in memory. One store per process; `resetStore` empties it for tests.
import { classify } from "./classify.js";

export const STATUSES = ["open", "closed"];

let tickets = [];
let nextId = 1;

export function resetStore() {
  tickets = [];
  nextId = 1;
}

export class ValidationError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

export function createTicket({ title, description } = {}) {
  const name = String(title ?? "").trim();
  if (!name) throw new ValidationError("title_required");
  const body = String(description ?? "");
  const { category } = classify(`${name} ${body}`);
  const ticket = {
    id: nextId++,
    title: name,
    description: body,
    category,
    status: "open",
    createdAt: new Date().toISOString(),
  };
  tickets.push(ticket);
  return { ...ticket };
}

export function listTickets() {
  return tickets.slice().reverse().map((t) => ({ ...t }));
}

export function getTicket(id) {
  const found = tickets.find((t) => t.id === Number(id));
  return found ? { ...found } : null;
}

export function setStatus(id, status) {
  if (!STATUSES.includes(status)) throw new ValidationError("invalid_status");
  const found = tickets.find((t) => t.id === Number(id));
  if (!found) return null;
  found.status = status;
  return { ...found };
}
