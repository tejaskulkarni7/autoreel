"""``autoreel`` command line: ``autoreel serve`` (default) and ``autoreel doctor``."""

from __future__ import annotations

import argparse
import sys


def doctor() -> int:
    from .config import get_server_settings
    from .pipeline.media import ffmpeg_available
    from .pipeline.transcribe import cuda_available

    s = get_server_settings()
    ok = True
    print(f"Data directory : {s.data_dir}")
    if ffmpeg_available():
        print("ffmpeg         : OK")
    else:
        ok = False
        print("ffmpeg         : MISSING - install it (brew install ffmpeg / apt install ffmpeg)")
    print(f"NVIDIA CUDA    : {'yes - Whisper will use the GPU' if cuda_available() else 'no - Whisper runs on CPU'}")
    print(f"Web UI built   : {'yes' if (s.frontend_dist / 'index.html').exists() else 'no - cd frontend && npm i && npm run build'}")
    try:
        import tiktok_uploader  # noqa: F401

        print("tiktok-uploader: installed (browser TikTok posting available)")
    except ImportError:
        print("tiktok-uploader: not installed (optional)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoreel", description="AutoReel - local AI shorts clipper & poster")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the web app (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")
    sub.add_parser("doctor", help="check the local setup")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        return doctor()

    import uvicorn

    from .config import get_server_settings

    s = get_server_settings()
    host = getattr(args, "host", None) or s.host
    port = getattr(args, "port", None) or s.port
    print(f"AutoReel running at http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run("autoreel.main:app", host=host, port=port, reload=getattr(args, "reload", False),
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
