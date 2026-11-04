// Hello World (template `hello-world`): one page that says hello and shows the time.
// No dependencies: Node's own http module. `node server.js` listens on PORT (default 3000).
const http = require("node:http");

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function page(now = new Date()) {
  const name = escapeHtml(process.env.HELLO_NAME || "World");
  const time = now.toISOString();
  return `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Hello World</title></head>
<body style="font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0;">
  <main>
    <h1>Hello, ${name}!</h1>
    <p>The time is <time datetime="${time}" data-testid="time">${time}</time></p>
  </main>
</body>
</html>`;
}

function handler(req, res) {
  if (req.method !== "GET" || (req.url !== "/" && !req.url.startsWith("/?"))) {
    res.statusCode = 404;
    res.end("Not found");
    return;
  }
  res.statusCode = 200;
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(page());
}

if (require.main === module) {
  const port = Number(process.env.PORT) || 3000;
  http.createServer(handler).listen(port, () => console.log(`Hello World on http://localhost:${port}`));
}

module.exports = { handler, page };
