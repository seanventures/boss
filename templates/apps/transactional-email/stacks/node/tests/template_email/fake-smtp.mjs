// An in-process fake SMTP server (plain text, loopback only): enough of RFC 5321 for nodemailer
// to deliver a message with AUTH PLAIN or LOGIN. No test reaches a real mail server.
//
// `failNext` makes the next DATA answer with a chosen code (451: try later, 550: rejected).
import net from "node:net";

export async function startFakeSmtp({ user = null, password = null } = {}) {
  const state = { messages: [], failNext: [], authFailures: 0, connections: 0 };

  const server = net.createServer((socket) => {
    state.connections++;
    let buffer = "";
    let mode = "command"; // command | data | auth-login-user | auth-login-pass
    let envelope = { from: null, to: [], authed: !user };
    let loginUser = "";
    const say = (line) => socket.write(`${line}\r\n`);
    const checkAuth = (u, p) => {
      if (u === user && p === password) {
        envelope.authed = true;
        return say("235 2.7.0 Authentication successful");
      }
      state.authFailures++;
      return say("535 5.7.8 Authentication failed");
    };

    say("220 fake-smtp.local ESMTP ready");
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      for (;;) {
        if (mode === "data") {
          const end = buffer.indexOf("\r\n.\r\n");
          if (end < 0) return;
          const raw = buffer.slice(0, end).replace(/\r\n\.\./g, "\r\n.");
          buffer = buffer.slice(end + 5);
          mode = "command";
          const code = state.failNext.shift();
          if (code) {
            say(`${code} ${code >= 500 ? "5.7.1 Message rejected" : "4.3.0 Try again later"}`);
          } else {
            state.messages.push({ from: envelope.from, to: [...envelope.to], raw });
            say("250 2.0.0 Ok: queued as FAKE" + state.messages.length);
          }
          envelope = { from: null, to: [], authed: envelope.authed };
          continue;
        }
        const nl = buffer.indexOf("\r\n");
        if (nl < 0) return;
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 2);
        if (mode === "auth-login-user") {
          loginUser = Buffer.from(line, "base64").toString();
          mode = "auth-login-pass";
          say("334 UGFzc3dvcmQ6");
          continue;
        }
        if (mode === "auth-login-pass") {
          mode = "command";
          checkAuth(loginUser, Buffer.from(line, "base64").toString());
          continue;
        }
        const verb = line.slice(0, 4).toUpperCase();
        if (verb === "EHLO" || verb === "HELO") {
          socket.write("250-fake-smtp.local\r\n250-AUTH PLAIN LOGIN\r\n250-8BITMIME\r\n250 SMTPUTF8\r\n");
        } else if (line.toUpperCase().startsWith("AUTH PLAIN")) {
          const parts = Buffer.from(line.slice(11).trim(), "base64").toString().split("\0");
          checkAuth(parts[1], parts[2]);
        } else if (line.toUpperCase().startsWith("AUTH LOGIN")) {
          mode = "auth-login-user";
          say("334 VXNlcm5hbWU6");
        } else if (verb === "MAIL") {
          if (!envelope.authed) say("530 5.7.0 Authentication required");
          else { envelope.from = (line.match(/<([^>]*)>/) || [])[1]; say("250 2.1.0 Ok"); }
        } else if (verb === "RCPT") {
          envelope.to.push((line.match(/<([^>]*)>/) || [])[1]);
          say("250 2.1.5 Ok");
        } else if (verb === "DATA") {
          mode = "data";
          say("354 End data with <CR><LF>.<CR><LF>");
        } else if (verb === "RSET") {
          envelope = { from: null, to: [], authed: envelope.authed };
          say("250 2.0.0 Ok");
        } else if (verb === "NOOP") {
          say("250 2.0.0 Ok");
        } else if (verb === "QUIT") {
          say("221 2.0.0 Bye");
          socket.end();
          return;
        } else {
          say("502 5.5.2 Command not recognized");
        }
      }
    });
    socket.on("error", () => {});
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    state,
    host: "127.0.0.1",
    port: server.address().port,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}
