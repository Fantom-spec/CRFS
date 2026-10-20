import argparse

from .app import serve


def main():
    p = argparse.ArgumentParser(prog="python -m webui", description="CRFS web UI (local only)")
    p.add_argument("--image-dir", default="crfs_images", help="directory that holds disk images (default ./crfs_images)")
    p.add_argument("--host", default="127.0.0.1", help="loopback address only (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the browser")
    a = p.parse_args()
    serve(a.image_dir, a.host, a.port, a.open)


if __name__ == "__main__":
    main()
