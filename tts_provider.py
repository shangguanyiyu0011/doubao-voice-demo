"""Volcengine V3 HTTP Chunked TTS. Only the Python process owns credentials."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from email.utils import parsedate_to_datetime

LOG = logging.getLogger("doubao.tts")
ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
MAX_TEXT_BYTES = 10000  # Local conservative limit, not a claimed upstream limit.
MAX_AUDIO_BYTES = 30 * 1024 * 1024


class TTSError(Exception):
    def __init__(self, code, message, status=502, retryable=False, retry_after=0):
        super().__init__(message)
        self.code, self.status = code, status
        self.retryable, self.retry_after = retryable, retry_after


class Cancelled(TTSError):
    def __init__(self):
        super().__init__("cancelled", "语音请求已取消。", 409)


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


def load_env(path, environ=None):
    values = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    values.update(os.environ if environ is None else environ)
    return values


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def valid_mp3(data):
    # Require an MPEG audio frame, not just an ID3 prefix/error payload.
    offset = 0
    if data.startswith(b"ID3") and len(data) >= 10:
        if any(b > 127 for b in data[6:10]):
            return False
        offset = 10 + sum(b << shift for b, shift in zip(data[6:10], (21, 14, 7, 0)))
    for i in range(offset, min(len(data) - 3, offset + 4096)):
        a, b, c = data[i:i + 3]
        if a == 255 and b & 224 == 224 and b & 24 != 8 and b & 6 and c >> 4 not in (0, 15) and c & 12 != 12:
            return True
    return False


def map_upstream_error(status, code, message):
    # Never return raw upstream bodies: they can echo credentials or user text.
    msg = str(message).lower()
    if status == 429 or "concurrency" in msg or "rate limit" in msg:
        return TTSError("rate_limit", "请求过于频繁或超出并发配额，请稍后重试。", 429, True)
    if any(x in msg for x in ("balance", "arrear", "insufficient", "quota", "credit")) or status == 402:
        return TTSError("balance", "余额或可用额度不足，请检查火山控制台。", 402)
    if "resource" in msg and any(x in msg for x in ("mismatch", "not found", "denied")):
        return TTSError("resource", "Resource ID 与音色不匹配、服务未开通或没有权限。", 400)
    if "speaker" in msg or code in (45002000, 45001115):
        return TTSError("voice", "音色 ID 不存在、未授权或与资源不匹配，请检查音色库。", 400)
    if status in (401, 403) or any(x in msg for x in ("token", "appid", "app-id", "access key", "api key", "auth")):
        return TTSError("auth", "凭据校验失败，请检查 API Key，或旧版 APPID / AccessToken。", 401)
    if code == 40402003 or "text" in msg and any(x in msg for x in ("limit", "long", "large")):
        return TTSError("text_limit", "回答超过语音服务长度限制，请拆成较短的轮次。", 400)
    if status >= 500 or str(code).startswith("5"):
        return TTSError("upstream", "豆包语音服务暂时不可用或超时，请稍后重试。", 502, True)
    return TTSError("parameters", "豆包语音生成失败，请检查语音服务配置及文本。", 400)


def retry_delay(header):
    try:
        return max(0, float(header))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(header).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class VolcengineTTSProvider:
    def __init__(self, config, transport=None):
        self.api_key = config.get("VOLCENGINE_TTS_API_KEY", "").strip()
        self.app_id = config.get("VOLCENGINE_TTS_APP_ID", "").strip()
        self.access_token = config.get("VOLCENGINE_TTS_ACCESS_TOKEN", "").strip()
        self.resource = config.get("VOLCENGINE_TTS_RESOURCE_ID", "seed-tts-2.0").strip()
        self.voice = config.get("VOLCENGINE_TTS_DEFAULT_VOICE", "zh_female_vv_uranus_bigtts").strip()
        self.sample_rate = int(config.get("VOLCENGINE_TTS_SAMPLE_RATE", "24000"))
        self.loudness = int(config.get("VOLCENGINE_TTS_LOUDNESS_RATE", "0"))
        if self.sample_rate not in (8000, 16000, 22050, 24000, 32000, 44100, 48000):
            raise ValueError("VOLCENGINE_TTS_SAMPLE_RATE 不在支持范围")
        if not -50 <= self.loudness <= 100:
            raise ValueError("VOLCENGINE_TTS_LOUDNESS_RATE 应在 -50 至 100")
        self.transport = transport or self._request
        self.verified = False

    @property
    def configured(self):
        return bool((self.api_key or self.app_id and self.access_token) and self.resource)

    def profile(self):
        return {"api": "v3-http-chunked", "resource": self.resource, "format": "mp3",
                "sample_rate": self.sample_rate, "loudness_rate": self.loudness,
                "enable_subtitle": True, "cache_version": 1}

    def status(self):
        return {"configured": self.configured, "connected": self.verified,
                "provider": "volcengine-doubao-tts", "voice": self.voice,
                "profile": self.profile(),
                "message": "豆包语音服务已连接" if self.verified else
                "凭据已配置，请播放试听验证连接" if self.configured else
                "未配置：请在项目 .env 中填写火山引擎凭据，保存后重启服务。"}

    def parameters(self, text, voice="", speed=1):
        if not isinstance(text, str) or not text.strip():
            raise TTSError("text", "回答文字不能为空。", 400)
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise TTSError("text_limit", "单轮回答超过本地 10000 UTF-8 字节上限，请拆成多轮。", 400)
        voice = (voice or self.voice) if isinstance(voice, str) else ""
        if not voice.strip() or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", voice.strip()):
            raise TTSError("voice", "请在 .env 或语音设置中填写有效的 Voice Type。", 400)
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise TTSError("speed", "语速须为 0.5 至 2.0 之间的数值。", 400)
        return {"text": text, "speaker": voice.strip(), "audio_params": {
            "format": "mp3", "sample_rate": self.sample_rate,
            "speech_rate": round((speed - 1) * 100), "loudness_rate": self.loudness,
            "enable_subtitle": True}}

    def cache_key(self, params):
        return hashlib.sha256(canonical({"profile": self.profile(), "params": params}).encode("utf-8")).hexdigest()

    def headers(self):
        headers = {"Content-Type": "application/json", "X-Api-Resource-Id": self.resource,
                   "X-Api-Request-Id": str(uuid.uuid4())}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            headers.update({"X-Api-App-Id": self.app_id, "X-Api-Access-Key": self.access_token})
        return headers

    def synthesize(self, params, cancel):
        if not self.configured:
            raise TTSError("not_configured", "火山语音账号未配置，请填写项目 .env 后重启。", 503)
        deadline = time.monotonic() + 100
        for attempt in range(3):
            check_cancel(cancel)
            try:
                result = self.transport(self.headers(), {"req_params": params}, cancel, deadline)
                check_cancel(cancel)
                self.verified = True
                return result
            except TTSError as error:
                if error.code == "auth":
                    self.verified = False
                delay = max(error.retry_after, .8 * 2 ** attempt)
                if not error.retryable or attempt == 2 or delay > 10 or time.monotonic() + delay >= deadline:
                    raise
                LOG.warning("retry=%s category=%s delay=%.2f", attempt + 1, error.code, delay)
                if cancel.wait(delay):
                    raise Cancelled()

    def _request(self, headers, body, cancel, deadline):
        conn = http.client.HTTPSConnection("openspeech.bytedance.com", timeout=8)
        finished = threading.Event()

        def watch():
            while not finished.wait(.1):
                if cancel.is_set() or time.monotonic() >= deadline:
                    try:
                        if conn.sock:
                            conn.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    conn.close()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            conn.connect()
            check_cancel(cancel)
            conn.sock.settimeout(25)
            conn.request("POST", "/api/v3/tts/unidirectional", canonical(body).encode("utf-8"), headers)
            response = conn.getresponse()
            logid = re.sub(r"[^A-Za-z0-9_-]", "", response.getheader("X-Tt-Logid", ""))[:100]
            LOG.info("upstream request_id=%s status=%s logid=%s", headers["X-Api-Request-Id"], response.status, logid)
            if response.status != 200:
                raw = response.read(65536)
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeError):
                    obj = {}
                err = map_upstream_error(response.status, obj.get("code"), obj.get("message", ""))
                err.retry_after = retry_delay(response.getheader("Retry-After"))
                LOG.warning("upstream code=%s category=%s logid=%s", obj.get("code"), err.code, logid)
                raise err
            return self.parse_stream(response, cancel, deadline)
        except (OSError, http.client.HTTPException) as error:
            check_cancel(cancel)
            LOG.warning("network failure type=%s", type(error).__name__)
            raise TTSError("network", "网络连接中断或 API 超时，请检查网络后重试。", 504, True) from None
        finally:
            finished.set()
            conn.close()
            watcher.join(timeout=.3)

    @staticmethod
    def parse_stream(response, cancel, deadline):
        audio = bytearray()
        words = []
        complete = False
        while True:
            check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise TTSError("timeout", "语音生成超时，请缩短回答或稍后重试。", 504)
            line = response.readline(4 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 4 * 1024 * 1024:
                raise TTSError("audio", "语音服务返回异常数据。")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict) or "code" not in obj:
                    raise ValueError()
                code = obj["code"]
                if code not in (0, 20000000):
                    error = map_upstream_error(200, code, obj.get("message", ""))
                    LOG.warning("stream code=%s category=%s", code, error.code)
                    raise error
                if obj.get("data"):
                    audio.extend(base64.b64decode(obj["data"], validate=True))
                sentence = obj.get("sentence")
                if isinstance(sentence, dict) and isinstance(sentence.get("words"), list):
                    words.extend(sentence["words"])
                if len(audio) > MAX_AUDIO_BYTES or len(words) > 50000:
                    raise ValueError()
                if code == 20000000:
                    complete = True
                    break
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise TTSError("audio", "音频数据格式异常，请重试。") from None
        if not complete or not valid_mp3(audio):
            raise TTSError("audio", "音频未完整返回或 MP3 数据异常，请重试。")
        return bytes(audio), words


def normalize_timings(words, text):
    timings, cursor, previous = [], 0, 0
    for word in words:
        if not isinstance(word, dict):
            continue
        token, start, end = word.get("word"), word.get("startTime"), word.get("endTime")
        if not isinstance(token, str) or not token or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)):
            continue
        at = text.find(token, cursor)
        if at < 0 or start < previous or end <= start:
            continue
        # Reject gaps containing pronounced characters: partial alignment isn't reliable.
        if any(c.isalnum() for c in text[cursor:at]):
            return []
        timings.append({"start": start, "end": end, "from": at, "to": at + len(token)})
        cursor, previous = at + len(token), end
    if any(c.isalnum() for c in text[cursor:]):
        return []
    return timings


class TTSCache:
    def __init__(self, root, provider):
        self.root, self.provider = Path(root), provider
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks = [threading.Lock() for _ in range(64)]
        self.slots = threading.BoundedSemaphore(2)

    def _read(self, key):
        try:
            path = self.root / (key + ".mp3")
            data = path.read_bytes()
            meta = json.loads((self.root / (key + ".json")).read_text(encoding="utf-8"))
            if not valid_mp3(data) or hashlib.sha256(data).hexdigest() != meta["sha256"]:
                return None
            return {"key": key, "url": "/api/tts/audio/" + key + ".mp3",
                    "timings": meta["timings"], "cache_hit": True}
        except (OSError, ValueError, KeyError):
            return None

    def get(self, text, voice, speed, cancel, lookup=False):
        params = self.provider.parameters(text, voice, speed)
        key = self.provider.cache_key(params)
        lock = self.locks[int(key[:4], 16) % len(self.locks)]
        while not lock.acquire(timeout=.1):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            cached = self._read(key)
            if cached or lookup:
                return cached or {"key": key, "cache_hit": False}
            while not self.slots.acquire(timeout=.1):
                check_cancel(cancel)
            try:
                check_cancel(cancel)
                audio, words = self.provider.synthesize(params, cancel)
                if not valid_mp3(audio):
                    raise TTSError("audio", "音频数据异常，未写入缓存。")
                timings = normalize_timings(words, text)
                check_cancel(cancel)
                temp = self.root / (key + ".tmp")
                meta_temp = self.root / (key + ".json.tmp")
                try:
                    temp.write_bytes(audio)
                    meta_temp.write_text(canonical({"sha256": hashlib.sha256(audio).hexdigest(), "timings": timings}), encoding="utf-8")
                    check_cancel(cancel)
                    temp.replace(self.root / (key + ".mp3"))
                    meta_temp.replace(self.root / (key + ".json"))
                finally:
                    temp.unlink(missing_ok=True)
                    meta_temp.unlink(missing_ok=True)
                LOG.info("generated cache_key=%s bytes=%s", key, len(audio))
                return {"key": key, "url": "/api/tts/audio/" + key + ".mp3", "timings": timings, "cache_hit": False}
            finally:
                self.slots.release()
        finally:
            lock.release()

    def clear(self):
        # Acquire the same locks as generation; never leave half a cache entry.
        for lock in self.locks:
            lock.acquire()
        try:
            count = 0
            for path in self.root.glob("*.mp3"):
                if re.fullmatch(r"[a-f0-9]{64}\.mp3", path.name):
                    path.unlink()
                    path.with_suffix(".json").unlink(missing_ok=True)
                    count += 1
            return count
        finally:
            for lock in reversed(self.locks):
                lock.release()
