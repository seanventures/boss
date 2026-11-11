// The tickets, kept in memory. One store per process; `resetStore` empties it for tests.
import { classify } from "./classify.js";

export const STATUSES = ["open", "closed"];
export const PRIORITIES = ["low", "normal", "high"];

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

export function createTicket({ title, description, priority } = {}) {
  if (!title) throw new ValidationError("title_required");
  const level = priority === undefined || priority === null ? "normal" : priority;
  if (!PRIORITIES.includes(level)) throw new ValidationError("invalid_priority");
  const body = String(description ?? "");
  const { category } = classify(`${title} ${body}`);
  const ticket = {
    id: nextId++,
    title: String(title),
    description: body,
    category,
    priority: level,
    status: "open",
    createdAt: new Date().toISOString(),
  };
  tickets.push(ticket);
  return { ...ticket };
}

export function listTickets({ priority } = {}) {
  if (priority !== undefined && !PRIORITIES.includes(priority)) {
    throw new ValidationError("invalid_priority");
  }
  return tickets
    .slice()
    .reverse()
    .filter((t) => priority === undefined || t.priority === priority)
    .map((t) => ({ ...t }));
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
