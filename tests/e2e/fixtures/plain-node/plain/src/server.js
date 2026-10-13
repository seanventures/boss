import { createServer } from "node:http";
import { slug } from "./slug.js";

const port = Number(process.env.PORT || 3000);
createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify({ slug: slug(url.searchParams.get("q")) }));
}).listen(port, "0.0.0.0");
