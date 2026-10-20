"""A tiny SMTP server for tests: records what it is sent, or refuses a step with a given reply.

smtpd left the standard library in 3.12 and aiosmtpd is not a dependency; this is the part of the
protocol smtplib speaks to a server without TLS or AUTH.
"""
from __future__ import annotations

import socketserver
import threading
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Optional


class FakeSMTP:
    def __init__(self, *, refuse_rcpt: Optional[str] = None):
        self.messages: list[EmailMessage] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        self.refuse_rcpt = refuse_rcpt
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            def handle(self) -> None:
                self.reply("220 fake.example.com ESMTP")
                sender, rcpts = "", []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    cmd = raw.decode("utf-8", "replace").strip()
                    verb = cmd.split(" ", 1)[0].upper()
                    if verb in ("EHLO", "HELO"):
                        self.reply("250-fake.example.com")
                        self.reply("250 8BITMIME")
                    elif verb == "MAIL":
                        sender, rcpts = cmd.split(":", 1)[1].strip(" <>").split(">")[0], []
                        self.reply("250 OK")
                    elif verb == "RCPT":
                        if outer.refuse_rcpt:
                            self.reply(outer.refuse_rcpt)
                        else:
                            rcpts.append(cmd.split(":", 1)[1].strip(" <>"))
                            self.reply("250 OK")
                    elif verb == "DATA":
                        self.reply("354 go ahead")
                        lines = []
                        while True:
                            line = self.rfile.readline()
                            if line in (b".\r\n", b".\n", b""):
                                break
                            lines.append(line[1:] if line.startswith(b"..") else line)
                        outer.messages.append(
                            message_from_bytes(b"".join(lines), policy=policy.default))
                        outer.envelopes.append((sender, rcpts))
                        self.reply("250 queued")
                    elif verb == "QUIT":
                        self.reply("221 bye")
                        return
                    else:  # RSET, NOOP, …
                        self.reply("250 OK")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeSMTP":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
