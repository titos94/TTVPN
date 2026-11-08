"""Local, Telegram-only HTTPS CONNECT tunnel through a trusted panel server."""
import logging
import select
import socketserver
import threading

logger = logging.getLogger(__name__)
_server = None
_lock = threading.Lock()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            part = self.request.recv(1024)
            if not part:
                return
            data += part
        if data.split(b"\r\n", 1)[0] != b"CONNECT api.telegram.org:443 HTTP/1.1":
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        channel = None
        try:
            import app as panel
            servers = panel.load_data().get("servers", [])
            index = self.server.server_index
            if index < 0 or index >= len(servers):
                raise ValueError("Telegram tunnel server not found")
            ssh = panel.get_ssh(servers[index])
            ssh.connect()
            transport = ssh.client.get_transport()
            channel = transport.open_channel("direct-tcpip", ("api.telegram.org", 443),
                                             ("127.0.0.1", 0), timeout=15)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(None)
            while True:
                ready, _, _ = select.select([self.request, channel], [], [], 60)
                if not ready:
                    continue
                for source in ready:
                    payload = source.recv(32768)
                    if not payload:
                        return
                    (channel if source is self.request else self.request).sendall(payload)
        except Exception:
            logger.exception("Telegram SSH tunnel failed")
        finally:
            if channel:
                channel.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(server_index: int) -> str:
    global _server
    with _lock:
        if _server is None:
            _server = _Server(("127.0.0.1", 0), _Handler)
            _server.server_index = server_index
            threading.Thread(target=_server.serve_forever, daemon=True,
                             name="telegram-ssh-tunnel").start()
        return "http://127.0.0.1:%d" % _server.server_address[1]
