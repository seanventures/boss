// GET /hello for a Node app (template `hello`). No dependencies: plug handleHello into the app's
// own server (Express: app.get("/hello", handleHello) also works, it only uses req/res basics).
function greeting() {
  return process.env.HELLO_GREETING || "hello from BOSS";
}

function handleHello(req, res) {
  if (req.method !== "GET") {
    res.statusCode = 405;
    res.setHeader("Allow", "GET");
    res.end();
    return;
  }
  res.statusCode = 200;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify({ message: greeting() }));
}

module.exports = { greeting, handleHello };
