"""Loopback-only local server; Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit
import webbrowser

from tts_provider import Cancelled, TTSCache, TTSError, VolcengineTTSProvider, load_env

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("doubao.server")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provider, root=ROOT):
        super().__init__(address, Handler)
        self.root = Path(root)
        self.provider = provider
        self.cache = TTSCache(self.root / "cache" / "tts", provider)
        self.token = secrets.token_urlsafe(32)  # Local CSRF nonce, never an upstream credential.
        self.jobs = {}
        self.jobs_lock = threading.Lock()

    def job(self, request_id, cancel=False):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise TTSError("request_id", "请求标识无效。", 400)
        with self.jobs_lock:
            now = time.monotonic()
            self.jobs = {k: v for k, v in self.jobs.items() if now - v[1] < 180}
            if request_id not in self.jobs:
                self.jobs[request_id] = (threading.Event(), now)
            event = self.jobs[request_id][0]
            if cancel:
                event.set()
            return event


class Handler(BaseHTTPRequestHandler):
    server_version = "DoubaoLocal/2.0"

    def log_message(self, format, *args):
        # Don't log URL query/body/credentials.
        LOG.info("local method=%s status=%s", self.command, args[1] if len(args) > 1 else "-")

    def allowed(self, mutation=False):
        host = self.headers.get("Host", "")
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in expected:
            raise TTSError("host", "仅允许本机访问。", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            raise TTSError("origin", "请从本地启动器打开页面。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise TTSError("origin", "不允许跨站访问。", 403)
        if mutation and not hmac.compare_digest(self.headers.get("X-Local-Token", ""), self.server.token):
            raise TTSError("local_token", "本地会话已失效，请刷新页面后重试。", 403)

    def send_bytes(self, code, data, mime="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, value):
        self.send_bytes(code, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def error(self, error):
        self.json(error.status, {"error": {"code": error.code, "message": str(error)}})

    def do_GET(self):
        try:
            self.allowed()
            path = unquote(urlsplit(self.path).path)
            if path == "/api/bootstrap":
                self.json(200, {"app": "voice-script-studio-local", "token": self.server.token})
            elif path == "/api/tts/status":
                self.json(200, self.server.provider.status())
            elif path in ("/", "/豆包虚拟问答系统.html"):
                self.send_bytes(200, (self.server.root / "豆包虚拟问答系统.html").read_bytes(), "text/html; charset=utf-8")
            elif re.fullmatch(r"/api/tts/audio/[a-f0-9]{64}\.mp3", path):
                file = self.server.cache.root / path.rsplit("/", 1)[1]
                if not file.is_file():
                    raise TTSError("missing", "缓存音频不存在，请重新生成。", 404)
                self.send_bytes(200, file.read_bytes(), "audio/mpeg")
            elif path in ("/tests/frontend.html", "/tests/frontend.js"):
                file = self.server.root / path.lstrip("/")
                self.send_bytes(200, file.read_bytes(), "text/html; charset=utf-8" if path.endswith("html") else "text/javascript; charset=utf-8")
            else:
                # Explicit allowlist: .env, source files, logs and directories are never served.
                raise TTSError("not_found", "未找到页面。", 404)
        except TTSError as error:
            self.error(error)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.error(TTSError("file", "无法读取本地文件。", 500))

    def do_POST(self):
        try:
            self.allowed(mutation=True)
            self.connection.settimeout(10)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536 or self.headers.get_content_type() != "application/json":
                raise TTSError("payload", "请求格式不正确或数据过大。", 400)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise TTSError("payload", "请求必须是 JSON 对象。", 400)
            path = urlsplit(self.path).path
            if path == "/api/tts/cancel":
                self.server.job(data.get("request_id"), cancel=True)
                self.json(200, {"cancelled": True})
            elif path in ("/api/tts/generate", "/api/tts/lookup"):
                event = self.server.job(data.get("request_id"))
                result = self.server.cache.get(data.get("text"), data.get("voice", ""), data.get("speed", 1), event, lookup=path.endswith("lookup"))
                self.json(200, result)
            elif path == "/api/tts/cache/clear":
                self.json(200, {"deleted": self.server.cache.clear()})
            else:
                raise TTSError("not_found", "接口不存在。", 404)
        except TTSError as error:
            self.error(error)
        except (ValueError, UnicodeError, TypeError):
            self.error(TTSError("payload", "请求 JSON 或字段格式错误。", 400))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            LOG.error("local failure type=%s", type(error).__name__, exc_info=True)
            self.error(TTSError("internal", "豆包语音生成失败，请检查本地服务日志。", 500))


def main():
    parser = argparse.ArgumentParser(description="豆包固定剧本语音演示")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--test-tts", action="store_true", help="人工真实测试，可能产生费用")
    args = parser.parse_args()
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(ROOT / "logs" / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=[handler])
    try:
        provider = VolcengineTTSProvider(load_env(ROOT / ".env"))
        if args.test_tts:
            cache = TTSCache(ROOT / "cache" / "tts", provider)
            result = cache.get("你好，我是豆包。语音已经准备好了，我们开始吧。", "", 1, threading.Event())
            print("试听文件：", cache.root / (result["key"] + ".mp3"))
            print("缓存命中" if result["cache_hit"] else "真实接口合成成功")
            return 0
        server = LocalServer(("127.0.0.1", args.port), provider)
        url = f"http://127.0.0.1:{server.server_port}/"
        print("系统已启动：", url, "\n关闭此窗口即可停止服务。", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            with server.jobs_lock:
                for event, _ in server.jobs.values():
                    event.set()
            server.server_close()
        return 0
    except (TTSError, ValueError, OSError) as error:
        print("启动或测试失败：", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
