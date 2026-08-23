#!/usr/bin/env python3
"""Simple HTTP proxy that routes through tun0 (VPN tunnel)."""

import socket
import threading
import sys

PROXY_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888


def handle_client(client_socket):
    """Forward HTTP requests through tun0."""
    try:
        request = client_socket.recv(4096).decode("utf-8", errors="replace")
        if not request:
            client_socket.close()
            return

        # Parse the request
        first_line = request.split("\n")[0]
        method = first_line.split(" ")[0]
        url = first_line.split(" ")[1]

        if method == "CONNECT":
            # HTTPS tunnel
            host, port = url.split(":")
            port = int(port)
            try:
                # Connect to target through tun0
                remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                remote.settimeout(30)
                # Force through tun0 by setting source interface
                remote.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun0")
                remote.connect((host, port))
                client_socket.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                # Forward data
                while True:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    remote.sendall(data)
                    data = remote.recv(4096)
                    if not data:
                        break
                    client_socket.sendall(data)
            except Exception as e:
                pass
            finally:
                try:
                    remote.close()
                except:
                    pass
        else:
            # HTTP request
            # Extract host from URL
            if url.startswith("http://"):
                host = url.split("/")[2]
                path = "/" + "/".join(url.split("/")[3:])
            else:
                host = first_line.split(" ")[1]
                path = "/"

            try:
                remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                remote.settimeout(30)
                remote.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun0")
                remote.connect((host, 80))

                # Rebuild request with just the path
                lines = request.split("\n")
                lines[0] = f"{method} {path} HTTP/1.1"
                new_request = "\n".join(lines)

                remote.sendall(new_request.encode())
                response = remote.recv(4096)
                client_socket.sendall(response)
            except Exception as e:
                pass
            finally:
                try:
                    remote.close()
                except:
                    pass
    except Exception as e:
        pass
    finally:
        try:
            client_socket.close()
        except:
            pass


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PROXY_PORT))
    server.listen(5)
    print(f"HTTP proxy listening on port {PROXY_PORT}")

    while True:
        client_socket, addr = server.accept()
        thread = threading.Thread(target=handle_client, args=(client_socket,))
        thread.daemon = True
        thread.start()


if __name__ == "__main__":
    main()
