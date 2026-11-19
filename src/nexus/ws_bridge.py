"""Adapt the daemon's WebSocket byte stream to the SDK's JSONL transport."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading

from websockets.sync.client import connect


def main() -> None:
    process = subprocess.Popen(
        sys.argv[1:],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    client_socket, transport_socket = socket.socketpair()

    def receive_transport() -> None:
        try:
            while data := process.stdout.read1(65536):
                transport_socket.sendall(data)
        except OSError:
            pass
        finally:
            try:
                transport_socket.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def send_transport() -> None:
        try:
            while data := transport_socket.recv(65536):
                process.stdin.write(data)
                process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    def stop(signum, frame) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=receive_transport, daemon=True).start()
    threading.Thread(target=send_transport, daemon=True).start()
    try:
        with connect(
            "ws://localhost/",
            sock=client_socket,
            proxy=None,
            compression=None,
            max_size=None,
            close_timeout=1,
            legacy=True,
        ) as websocket:

            def send_input() -> None:
                try:
                    for line in sys.stdin:
                        websocket.send(line.rstrip("\n"))
                finally:
                    websocket.close()

            threading.Thread(target=send_input, daemon=True).start()
            for message in websocket:
                sys.stdout.write(message + "\n")
                sys.stdout.flush()
    finally:
        client_socket.close()
        transport_socket.close()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
