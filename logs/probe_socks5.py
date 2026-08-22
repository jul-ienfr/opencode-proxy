"""Probe the gluetun SOCKS5 tunnel (port 1080) with a raw handshake."""
import socket
import struct


def try_connect(host: str, port: int) -> None:
    print(f"--- {host}:{port} ---")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((host, port))
        print("TCP connect OK")
        # SOCKS5 greeting: version 5, 1 method, no-auth
        s.sendall(b"\x05\x01\x00")
        resp = s.recv(4)
        print("greeting response:", resp.hex(), resp)
        if len(resp) >= 2 and resp[0] == 0x05:
            # CONNECT to api.ipify.org:443
            domain = b"api.ipify.org"
            req = b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + struct.pack(">H", 443)
            s.sendall(req)
            resp2 = s.recv(32)
            print("connect response:", resp2.hex(), resp2)
    except Exception as e:
        print("FAIL:", type(e).__name__, str(e)[:200])
    finally:
        s.close()


if __name__ == "__main__":
    try_connect("127.0.0.1", 1080)
    try_connect("127.0.0.1", 8888)
