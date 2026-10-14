export function getHost(): string {
  return process.env.HOST || "0.0.0.0";
}

export function getPort(): number {
  return Number(process.env.PORT || 3000);
}
