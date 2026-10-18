"""Bounded, root-local Agent HTTP transport; no TCP, redirects, proxies or retries."""
import http.client
import json
import socket


SOCKET = '/run/deploy-platform/agent.sock'


class AgentError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('Agent request failed: HTTP ' + str(status))


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path=SOCKET, timeout=5):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path, method='GET', socket_path=SOCKET):
    connection = UnixHTTP(socket_path)
    try:
        connection.request(method, path, headers={'Connection': 'close'})
        response = connection.getresponse()
        data = response.read(1024 * 1024 + 1)
        if response.status != 200:
            raise AgentError(response.status)
        if len(data) > 1024 * 1024:
            raise ValueError('oversized Agent response')
        return json.loads(data)
    finally:
        connection.close()
