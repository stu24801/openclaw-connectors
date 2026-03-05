import socket
import threading
import argparse
import sys


def forward(src: socket.socket, dst_host: str, dst_port: int) -> None:
    try:
        d = socket.socket()
        d.connect((dst_host, dst_port))

        def relay(a: socket.socket, b: socket.socket) -> None:
            try:
                while True:
                    data = a.recv(4096)
                    if not data:
                        break
                    b.sendall(data)
            except Exception:
                pass
            finally:
                for sock in (a, b):
                    try:
                        sock.close()
                    except Exception:
                        pass

        threading.Thread(target=relay, args=(src, d), daemon=True).start()
        threading.Thread(target=relay, args=(d, src), daemon=True).start()
    except Exception as e:
        print(f"[portforward] connect error: {e}", flush=True)
        src.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TCP port-forward: bind LISTEN_HOST:LISTEN_PORT → DST_HOST:DST_PORT"
    )
    parser.add_argument("--listen-host", default="172.18.0.1",
                        help="IP to bind (default: 172.18.0.1 = docker bridge gateway)")
    parser.add_argument("--listen-port", type=int, default=9090,
                        help="Port to listen on (default: 9090)")
    parser.add_argument("--dst-host", default="127.0.0.1",
                        help="Destination host (default: 127.0.0.1)")
    parser.add_argument("--dst-port", type=int, default=9000,
                        help="Destination port (default: 9000)")
    args = parser.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.listen_host, args.listen_port))
    except OSError as e:
        print(f"[portforward] bind failed: {e}", file=sys.stderr)
        sys.exit(1)

    srv.listen(50)
    print(
        f"[portforward] listening on {args.listen_host}:{args.listen_port} "
        f"→ {args.dst_host}:{args.dst_port}",
        flush=True,
    )

    while True:
        client, addr = srv.accept()
        threading.Thread(
            target=forward,
            args=(client, args.dst_host, args.dst_port),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
