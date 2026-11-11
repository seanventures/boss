import { createServer } from "node:http";
import { handle } from "./app.js";

const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || "0.0.0.0";
createServer(handle).listen(port, host);
