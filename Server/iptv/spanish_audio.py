"""Local Spanish audio triage for the English IPTV trial.

The sampler reuses media bytes that the gateway already fetched for the exact
provider identity, decodes them locally with ffmpeg, performs energy-based
speech detection and then runs whisper.cpp (multilingual model) locally for
language identification. No audio, transcript, media URL or credential is
stored or sent anywhere. Temporary PCM/WAV files are deleted after use.

The automatic decision is deliberately conservative. It can only add a
Spanish mark; it never removes a mark, never overrides a manual decision and
never mutates rotation, policy, hiatus, Favorites or playback history.
"""
import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

REVISION = "spanish-auto-v1"
RATE = 16000
WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000
BYTES_PER_SECOND = RATE * WIDTH
MAX_MAP_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_PCM_SECONDS = 75.0
TMP_PREFIX = "jealousfin-spanish-"

# Conservative automatic-mark thresholds. These are intentionally stricter
# than a single-window whisper heuristic: several separated speech-bearing
# windows must agree, and a Portuguese/Spanish lexical check must not object.
TARGET_SECONDS = 55.0
MAX_SECONDS = 70.0
MIN_SAMPLE_SECONDS = 40.0
MIN_SPEECH_SECONDS = 18.0
MIN_COVERAGE = 0.15
MIN_WINDOWS = 3
MIN_SPANISH_WINDOWS = 3
MIN_WINDOW_SECONDS = 8.0
MIN_WINDOW_SPEECH_RATIO = 0.18
MIN_WINDOW_SPEECH_SECONDS = 2.5
MIN_WINDOW_PROBABILITY = 0.78
MIN_SPANISH_PROBABILITY = 0.82
STRICT_NO_LEXICAL_PROBABILITY = 0.92
MIN_LEXICAL_MARGIN = -0.05
WINDOW_SECONDS = 15.0
MAX_WINDOWS = 4
IDLE_SECONDS = 30.0
SWITCH_GRACE_SECONDS = 4.0
VISIT_GAP_SECONDS = 6.0
FINALIZE_COOLDOWN_SECONDS = 180.0
MIN_REPORT_SECONDS = 15.0
PROBE_INTERVAL_SECONDS = 10.0
INACTIVE_SECONDS = 180.0
DETECT_TIMEOUT = 25
TRANSCRIBE_TIMEOUT = 75
DECODE_TIMEOUT = 15
MAX_DECODE_SECONDS = 12.0

_SPANISH = {
    "y", "el", "la", "los", "las", "del", "al", "es", "son", "una", "unos", "unas",
    "pero", "muy", "más", "usted", "ustedes", "gracias", "hola", "también",
    "después", "años", "niños", "están", "será", "han", "hay", "porque", "cuando",
    "donde", "quien", "esto", "eso", "aquello", "señor", "señora", "deporte",
    "inversión", "equipo", "partido", "calle", "celebrar", "vecinos",
    "organizadores", "invitaron", "ciudadanos", "actividades", "gratuitas",
    "soleado", "agradables", "bienvenidos", "noticiero", "tarde", "hoy",
    "hablaremos", "gobierno", "anunció", "nuevas", "medidas", "apoyar", "familias",
    "pequeños", "comercios", "expertos", "señalan", "situación", "mejorará",
    "próximos", "meses", "continúa", "noticias", "nacional", "ganó", "ayer",
    "último", "minuto", "muchas", "personas", "salieron", "compartir", "amigos",
    "abrirá", "nuevo", "museo", "dedicado", "historia", "región", "invitaron",
    "participar", "clima", "domingo", "temperaturas",
}
_PORTUGUESE = {
    "e", "o", "os", "as", "do", "da", "dos", "das", "é", "são", "um", "uma",
    "uns", "umas", "muito", "mas", "mais", "você", "vocês", "obrigado", "olá",
    "agora", "governo", "crianças", "estão", "foi", "há", "onde", "quem", "isto",
    "isso", "aquilo", "senhor", "senhora", "esporte", "investimento", "equipe",
    "partida", "rua", "comemorar", "vizinhos", "convidaram", "cidadãos",
    "ensolarado", "agradáveis", "bem-vindos", "noticiário", "noite", "hoje",
    "vamos", "falar", "anunciou", "novas", "medidas", "apoiar", "famílias",
    "pequenos", "negócios", "especialistas", "dizem", "situação", "vai",
    "melhorar", "próximos", "meses", "continua", "notícias", "nacional",
    "venceu", "ontem", "último", "muitas", "pessoas", "saíram", "ruas",
    "compartilhar", "amigos", "abrir", "novo", "museu", "dedicado", "história",
    "região", "participar", "tempo", "ficar", "domingo", "temperaturas",
}
_OVERLAP = _SPANISH & _PORTUGUESE
_SPANISH -= _OVERLAP
_PORTUGUESE -= _OVERLAP
_TOKEN = re.compile(r"[a-záéíóúüñçãõâêôà]+")
_DETECT = re.compile(r"auto-detected language:\s*([a-z]{2,3})\s*\(p\s*=\s*([0-9.]+)\)", re.IGNORECASE)


def _write_wav(path, pcm):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(WIDTH)
        handle.setframerate(RATE)
        handle.writeframes(pcm)


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def analyze_speech(pcm, window_seconds=WINDOW_SECONDS, max_windows=MAX_WINDOWS):
    """Energy-based speech evidence over decoded 16 kHz mono PCM.

    Uses only local PCM. Returns duration, active speech seconds, overall
    coverage and up to four separated windows spanning the sample.
    """
    usable_bytes = len(pcm) - (len(pcm) % (FRAME_SAMPLES * WIDTH))
    samples = array.array("h")
    if usable_bytes > 0:
        samples.frombytes(pcm[:usable_bytes])
    duration = len(samples) / RATE
    if not samples:
        return {"duration": 0.0, "speechSeconds": 0.0, "coverage": 0.0, "windows": []}
    energies = []
    for start in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        total = 0
        for value in samples[start:start + FRAME_SAMPLES]:
            total += value * value
        energies.append(math.sqrt(total / FRAME_SAMPLES) / 32768.0)
    floor = _percentile(energies, 0.10)
    peak = _percentile(energies, 0.95)
    threshold = max(floor * 2.5, peak * 0.08, 0.004)
    active = [value > threshold for value in energies]
    active_count = sum(active)
    speech_seconds = active_count * FRAME_MS / 1000.0
    coverage = active_count / len(active) if active else 0.0
    count = max(1, min(max_windows, int(duration // MIN_WINDOW_SECONDS))) if duration >= MIN_WINDOW_SECONDS else 1
    span = min(window_seconds, duration / count) if count else duration
    windows = []
    for index in range(count):
        start_seconds = index * span
        start_frame = int(start_seconds * 1000 / FRAME_MS)
        end_frame = min(len(active), int((start_seconds + span) * 1000 / FRAME_MS))
        frames = active[start_frame:end_frame]
        window_speech = sum(frames) * FRAME_MS / 1000.0
        ratio = (sum(frames) / len(frames)) if frames else 0.0
        windows.append({
            "index": index, "start": round(start_seconds, 3), "duration": round(span, 3),
            "speechSeconds": round(window_speech, 3), "speechRatio": round(ratio, 3),
            "usable": span >= MIN_WINDOW_SECONDS and ratio >= MIN_WINDOW_SPEECH_RATIO and window_speech >= MIN_WINDOW_SPEECH_SECONDS,
        })
    return {
        "duration": round(duration, 3), "speechSeconds": round(speech_seconds, 3),
        "coverage": round(coverage, 4), "windows": windows,
    }


def looks_plain(data):
    """True for recognizable plaintext TS/fMP4/ADTS/MP3 media magic."""
    head = data[:32]
    if not head:
        return False
    if head[0] == 0x47:
        return True
    if b"ftyp" in head or b"styp" in head or b"moof" in head:
        return True
    if len(head) > 1 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0:
        return True
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
        return True
    return False


def _aes_decrypt(data, key, iv):
    """AES-CBC decrypt one HLS AES-128/256 segment. Key stays in memory."""
    if not data or len(data) % 16 or len(key) not in (16, 24, 32) or len(iv) != 16:
        return b""
    plain = None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
    except Exception:
        try:
            from Crypto.Cipher import AES
            plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        except Exception:
            return b""
    if not plain:
        return b""
    pad = plain[-1]
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def _iv_bytes(iv, sequence):
    if isinstance(iv, str) and iv:
        value = iv[2:] if iv.lower().startswith("0x") else iv
        try:
            raw = bytes.fromhex(value)
            if len(raw) == 16:
                return raw
        except ValueError:
            pass
    if isinstance(sequence, int) and 0 <= sequence < (1 << 128):
        return sequence.to_bytes(16, "big")
    return None


def decode_audio(data, map_data, ffmpeg, tmpdir, timeout=DECODE_TIMEOUT, key=None, iv=None, sequence=None):
    """Decode one complete media segment to 16 kHz mono PCM using ffmpeg.

    If the segment is an HLS AES-128/256 encrypted blob and the session key is
    supplied in memory, decrypt it first. The key is never written or logged.
    """
    if not data or len(data) > MAX_SEGMENT_BYTES:
        return b""
    if key and not looks_plain(data):
        decrypted = _aes_decrypt(data, key, _iv_bytes(iv, sequence))
        if decrypted:
            data = decrypted
    head = data[:32]
    if head.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"#EXTM3U"):
        return b""
    is_transport = bool(head) and head[0] == 0x47
    is_fragmented = b"ftyp" in head or b"styp" in head or b"moof" in head
    suffix = ".ts" if is_transport else (".mp4" if is_fragmented else ".bin")
    payload = data
    if map_data and not is_transport and (is_fragmented or b"moof" in head):
        if map_data not in data[:len(map_data)]:
            payload = map_data + data
            suffix = ".mp4"
    path = Path(tmpdir) / (uuid.uuid4().hex + suffix)
    try:
        path.write_bytes(payload)
        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
            "-t", str(MAX_DECODE_SECONDS), "-f", "s16le", "-",
        ]
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if completed.returncode != 0 or not completed.stdout:
            # Some providers start a segment mid-stream or use a sparse header.
            # Retry once with permissive demuxing; still local, still bounded.
            command = [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
                "-err_detect", "ignore_err", "-probesize", "32M", "-analyzeduration", "32M",
                "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
                "-t", str(MAX_DECODE_SECONDS), "-f", "s16le", "-",
            ]
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if completed.returncode != 0:
            return b""
        return completed.stdout[:int(MAX_DECODE_SECONDS * BYTES_PER_SECOND)]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return b""
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def detect_language(wav, whisper, model, threads=4, timeout=DETECT_TIMEOUT):
    """Return (language, probability) from whisper.cpp auto language ID."""
    command = [whisper, "-m", model, "-f", str(wav), "-l", "auto", "-dl", "-t", str(threads)]
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", "replace")
    match = None
    for match in _DETECT.finditer(text):
        pass
    if not match:
        return None
    try:
        probability = float(match.group(2))
    except ValueError:
        return None
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        return None
    return match.group(1).lower(), probability


def transcribe(wav, whisper, model, tmpdir, threads=4, timeout=TRANSCRIBE_TIMEOUT):
    """Return (language, text) without persisting or logging the transcript."""
    base = Path(tmpdir) / ("full-" + uuid.uuid4().hex)
    command = [
        whisper, "-m", model, "-f", str(wav), "-l", "auto", "-oj",
        "-of", str(base), "-t", str(threads), "-np",
    ]
    try:
        completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        if completed.returncode != 0:
            return None
        path = base.with_suffix(".json")
        if not path.exists() or path.stat().st_size > 8 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        language = data.get("result", {}).get("language")
        rows = data.get("transcription", [])
        text = " ".join(str(row.get("text", "")) for row in rows if isinstance(row, dict))
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language):
            return None
        return language.lower(), text[:40000]
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            base.with_suffix(".json").unlink()
        except OSError:
            pass


def lexical_margin(text):
    """Distinctive Spanish-minus-Portuguese word balance in [-1, 1]."""
    if not text:
        return None, 0, 0
    tokens = _TOKEN.findall(text.lower())
    spanish = sum(1 for token in tokens if token in _SPANISH)
    portuguese = sum(1 for token in tokens if token in _PORTUGUESE)
    total = spanish + portuguese
    if total < 2:
        return None, spanish, portuguese
    return round((spanish - portuguese) / total, 4), spanish, portuguese


def decide(analysis, window_results, lexical, config=None):
    """Conservative aggregation. Returns outcome/reason/confidence/evidence."""
    values = {
        "min_sample_seconds": MIN_SAMPLE_SECONDS, "min_speech_seconds": MIN_SPEECH_SECONDS,
        "min_coverage": MIN_COVERAGE, "min_windows": MIN_WINDOWS,
        "min_spanish_windows": MIN_SPANISH_WINDOWS, "min_window_probability": MIN_WINDOW_PROBABILITY,
        "min_spanish_probability": MIN_SPANISH_PROBABILITY,
        "strict_no_lexical_probability": STRICT_NO_LEXICAL_PROBABILITY,
        "min_lexical_margin": MIN_LEXICAL_MARGIN,
    }
    if config:
        values.update(config)
    usable = [w for w in window_results if w.get("usable") and w.get("language") and w.get("probability") is not None]
    if analysis["duration"] < values["min_sample_seconds"]:
        return _decision("inconclusive", "sample_too_short", 0.0, analysis, window_results, lexical)
    if analysis["speechSeconds"] < values["min_speech_seconds"]:
        return _decision("inconclusive", "insufficient_speech", 0.0, analysis, window_results, lexical)
    if analysis["coverage"] < values["min_coverage"]:
        return _decision("inconclusive", "low_speech_coverage", 0.0, analysis, window_results, lexical)
    if len(usable) < values["min_windows"]:
        return _decision("inconclusive", "insufficient_speech_windows", 0.0, analysis, window_results, lexical)
    spanish = [w for w in usable if w["language"] == "es" and w["probability"] >= values["min_window_probability"]]
    other = [w for w in usable if w["language"] != "es" and w["probability"] >= values["min_window_probability"]]
    low = [w for w in usable if w["probability"] < values["min_window_probability"]]
    if other:
        if not spanish:
            languages = sorted({w["language"] for w in other})
            reason = "language_" + languages[0] if len(languages) == 1 else "other_language"
            return _decision("non_spanish", reason, 0.0, analysis, window_results, lexical)
        return _decision("inconclusive", "mixed_language", 0.0, analysis, window_results, lexical)
    if len(spanish) < values["min_spanish_windows"] or low:
        return _decision("inconclusive", "ambiguous_language", 0.0, analysis, window_results, lexical)
    mean_probability = sum(w["probability"] for w in spanish) / len(spanish)
    if mean_probability < values["min_spanish_probability"]:
        return _decision("inconclusive", "low_language_confidence", mean_probability, analysis, window_results, lexical)
    margin = lexical[0] if lexical else None
    if margin is not None and margin < values["min_lexical_margin"]:
        return _decision("inconclusive", "spanish_portuguese_ambiguous", mean_probability, analysis, window_results, lexical)
    if margin is None and mean_probability < values["strict_no_lexical_probability"]:
        return _decision("inconclusive", "lexical_check_unavailable", mean_probability, analysis, window_results, lexical)
    return _decision("spanish", "spanish_dominant", mean_probability, analysis, window_results, lexical)


def _decision(outcome, reason, confidence, analysis, window_results, lexical):
    margin = lexical[0] if lexical else None
    return {
        "outcome": outcome, "reason": reason,
        "confidence": round(float(confidence), 4),
        "speechSeconds": analysis["speechSeconds"],
        "sampleSeconds": analysis["duration"],
        "speechCoverage": analysis["coverage"],
        "windows": len(window_results),
        "languages": [
            {"language": w["language"], "probability": round(float(w["probability"]), 4)}
            for w in window_results if w.get("language") and w.get("probability") is not None
        ],
        "lexicalMargin": margin,
    }


class _Base:
    __slots__ = ("base", "is_audio", "pcm", "seconds", "seen", "segments", "failures", "last_tap", "started")

    def __init__(self, base, is_audio, now):
        self.base = base
        self.is_audio = bool(is_audio)
        self.pcm = bytearray()
        self.seconds = 0.0
        self.seen = set()
        self.segments = 0
        self.failures = 0
        self.last_tap = now
        self.started = now


class _Sample:
    __slots__ = ("cid", "provider", "name", "bases", "started", "last_tap", "probe", "probe_done")

    def __init__(self, cid, provider, name, now):
        self.cid = cid
        self.provider = provider
        self.name = name
        self.bases = {}
        self.started = now
        self.last_tap = now
        self.probe = None
        self.probe_done = False


class Manager:
    """Background sampler and conservative classifier.

    The gateway calls tap() with bytes it already fetched. Two daemon threads
    decode and classify. Telemetry contains no audio, transcript, URL or
    credential.
    """

    def __init__(self, root, model_path, on_spanish, control, probe=None, log=None,
                 ffmpeg=None, whisper=None, threads=4):
        self.root = Path(root)
        self.model_path = str(model_path)
        self.on_spanish = on_spanish
        self.control = control
        self.probe = probe
        self.log = log or (lambda message: None)
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        self.whisper = whisper or shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        self.threads = max(1, min(8, int(threads)))
        self.state_file = self.root / "spanish-auto-state.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._taps = None
        self._jobs = None
        self._samples = {}
        self._finalized = {}
        self._maps = {}
        self._keys = {}
        self._control_cache = (0.0, True)
        self._last_media = 0.0
        self._probe_cid = None
        self._probe_at = 0.0
        self._tmpdir = None
        self._last_persist = 0.0
        self._diag_written = False
        self._sweep_stale()
        self.fault = self._check_tools()
        self.telemetry = self._load_state()

    def _check_tools(self):
        if not Path(self.model_path).is_file():
            return "model_missing"
        if not Path(self.model_path).stat().st_size > 1024 * 1024:
            return "model_invalid"
        if not Path(self.ffmpeg).exists():
            return "ffmpeg_missing"
        if not Path(self.whisper).exists():
            return "whisper_missing"
        return None

    def _empty_state(self):
        return {
            "schemaVersion": 1,
            "revision": REVISION,
            "updatedAt": 0,
            "counters": {
                "tested": 0, "spanish": 0, "nonSpanish": 0, "inconclusive": 0,
                "errors": 0, "suppressed": 0, "segmentsDecoded": 0,
                "segmentsRejected": 0, "segmentsDecrypted": 0, "keyMissing": 0,
                "samplesFinalized": 0,
            },
            "last": None,
            "current": None,
            "fault": self.fault,
        }

    def _load_state(self):
        state = self._empty_state()
        try:
            if self.state_file.exists() and self.state_file.stat().st_size <= 128 * 1024:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if raw.get("schemaVersion") == 1 and raw.get("revision") == REVISION:
                    counters = raw.get("counters")
                    if isinstance(counters, dict):
                        for key in state["counters"]:
                            if type(counters.get(key)) is int and counters[key] >= 0:
                                state["counters"][key] = counters[key]
                    if isinstance(raw.get("last"), dict):
                        state["last"] = raw["last"]
        except (OSError, ValueError, TypeError):
            pass
        state["fault"] = self.fault
        return state

    def _persist(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_persist < 5:
            return
        self._last_persist = now
        with self._lock:
            snapshot = json.loads(json.dumps(self.telemetry))
        snapshot["updatedAt"] = time.time()
        path = self.state_file
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _sweep_stale(self):
        for base in {tempfile.gettempdir(), "/tmp"}:
            try:
                for entry in Path(base).glob(TMP_PREFIX + "*"):
                    try:
                        if entry.is_dir() and time.time() - entry.stat().st_mtime > 3600:
                            shutil.rmtree(entry, ignore_errors=True)
                    except OSError:
                        pass
            except OSError:
                pass

    def refresh_control(self):
        with self._lock:
            try:
                value = bool(self.control())
            except Exception:
                value = False
            self._control_cache = (time.monotonic(), value)
            return value

    def _enabled(self):
        now = time.monotonic()
        cached_at, value = self._control_cache
        if now - cached_at < 2:
            return value
        try:
            value = bool(self.control())
        except Exception:
            value = False
        self._control_cache = (now, value)
        return value

    def start(self):
        if self.fault:
            self.log("spanish auto disabled " + self.fault)
            return False
        self._tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
        os.chmod(self._tmpdir, 0o700)
        self._taps = __import__("queue").Queue(maxsize=512)
        self._jobs = __import__("queue").Queue(maxsize=8)
        for target, name in ((self._decode_loop, "spanish-audio-decode"), (self._infer_loop, "spanish-audio-infer")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
        self.log("spanish auto ready model=" + Path(self.model_path).name)
        return True

    def stop(self):
        self._stop.set()
        for queue in (self._taps, self._jobs):
            if queue is not None:
                try:
                    queue.put_nowait(None)
                except Exception:
                    pass
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def tap(self, cid, provider, name, rid, base, data, is_audio, content_type="", correlation=None,
            key_bytes=None, key_rid=None, iv=None, sequence=None):
        """Enqueue one already-fetched media segment. Never blocks or raises."""
        if self.fault or not cid or not provider or not base or not data:
            return
        now = time.time()
        with self._lock:
            self._last_media = now
        if not self._enabled():
            return
        if len(data) > MAX_SEGMENT_BYTES:
            return
        if data.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"#EXTM3U"):
            return
        try:
            self._taps.put_nowait({
                "cid": cid, "provider": provider, "name": name, "rid": rid, "base": base,
                "data": data, "is_audio": bool(is_audio), "contentType": content_type,
                "keyBytes": key_bytes, "keyRid": key_rid, "iv": iv, "sequence": sequence,
                "wall": now,
            })
        except Exception:
            with self._lock:
                self.telemetry["counters"]["segmentsRejected"] += 1

    def tap_map(self, base, data):
        if self.fault or not base or not data or len(data) > MAX_MAP_BYTES:
            return
        if len(self._maps) >= 256 and base not in self._maps:
            try:
                self._maps.pop(next(iter(self._maps)))
            except (StopIteration, KeyError):
                pass
        self._maps[base] = bytes(data)

    def tap_key(self, key_rid, data):
        if self.fault or not key_rid or not data or not 16 <= len(data) <= 64:
            return
        if len(self._keys) >= 256 and key_rid not in self._keys:
            try:
                self._keys.pop(next(iter(self._keys)))
            except (StopIteration, KeyError):
                pass
        self._keys[key_rid] = bytes(data)

    def _probe(self):
        if self.probe is None:
            return None
        try:
            result = self.probe()
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    def _decode_loop(self):
        while not self._stop.is_set():
            try:
                event = self._taps.get(timeout=0.5)
            except Exception:
                self._housekeeping()
                continue
            if event is None:
                continue
            try:
                self._decode_event(event)
            except Exception as error:
                self.log("spanish auto decode error " + type(error).__name__)
            self._housekeeping()

    def _decode_event(self, event):
        if not self._enabled():
            return
        now = event["wall"]
        sample = self._samples.get(event["cid"])
        if sample and now - sample.last_tap > VISIT_GAP_SECONDS:
            self._finalize(event["cid"], "visit_gap")
            sample = None
        if sample is None:
            last = self._finalized.get(event["cid"])
            if last and now - last < FINALIZE_COOLDOWN_SECONDS:
                return
            sample = _Sample(event["cid"], event["provider"], event["name"], now)
            self._samples[event["cid"]] = sample
        sample.last_tap = now
        if event["name"] and not sample.name:
            sample.name = event["name"]
        if not sample.probe_done:
            sample.probe_done = True
            sample.probe = self._probe()
            if sample.probe and sample.probe.get("channelID"):
                self._probe_cid = sample.probe["channelID"]
                self._probe_at = now
        base = sample.bases.get(event["base"])
        if base is None:
            base = _Base(event["base"], event["is_audio"], now)
            sample.bases[event["base"]] = base
        base.is_audio = base.is_audio or bool(event["is_audio"])
        base.last_tap = now
        if event["rid"] in base.seen:
            return
        base.seen.add(event["rid"])
        map_data = self._maps.get(event["base"])
        key_bytes = event.get("keyBytes") or self._keys.get(event.get("keyRid"))
        plain = looks_plain(event["data"])
        pcm = decode_audio(event["data"], map_data, self.ffmpeg, self._tmpdir,
                           key=key_bytes, iv=event.get("iv"), sequence=event.get("sequence"))
        if not pcm:
            base.failures += 1
            self._write_diagnostic(event, map_data)
            with self._lock:
                self.telemetry["counters"]["segmentsRejected"] += 1
                if not plain and event.get("keyRid") and not key_bytes:
                    self.telemetry["counters"]["keyMissing"] += 1
            return
        if key_bytes and not plain:
            with self._lock:
                self.telemetry["counters"]["segmentsDecrypted"] += 1
        if base.seconds < MAX_SECONDS:
            room = int((MAX_PCM_SECONDS - base.seconds) * BYTES_PER_SECOND)
            if len(pcm) > room:
                pcm = pcm[:max(0, room)]
            base.pcm.extend(pcm)
            base.seconds = len(base.pcm) / BYTES_PER_SECOND
            base.segments += 1
            with self._lock:
                self.telemetry["counters"]["segmentsDecoded"] += 1
        if base.seconds >= TARGET_SECONDS:
            self._finalize(event["cid"], "target_reached")
        self._update_current()

    def _write_diagnostic(self, event, map_data):
        """Capture one bounded, credential-free sample for local diagnosis.

        It contains only media bytes (no URL, key or credential) and is meant
        to be inspected and deleted. Status exposes only size/magic metadata.
        """
        if self._diag_written:
            return
        self._diag_written = True
        try:
            base = Path(tempfile.gettempdir()) / "jf-spanish-decode-diag"
            base.mkdir(mode=0o700, exist_ok=True)
            sample = base / "segment.bin"
            sample.write_bytes(event["data"][:262144])
            os.chmod(sample, 0o600)
            head = event["data"][:16]
            meta = {
                "at": round(time.time(), 3), "size": len(event["data"]),
                "magicHex": head.hex(),
                "magicAscii": "".join(chr(value) if 32 <= value < 127 else "." for value in head),
                "contentType": str(event.get("contentType", ""))[:80],
                "hasMap": bool(map_data), "mapSize": len(map_data or b""),
                "isAudio": bool(event.get("is_audio")),
                "baseHash": hashlib.sha256(event["base"].encode("utf-8", "replace")).hexdigest()[:16],
            }
            (base / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
            with self._lock:
                self.telemetry["diagnostic"] = meta
            self.log("Spanish auto decode diagnostic size=%d magic=%s" % (meta["size"], meta["magicAscii"]))
        except OSError:
            pass

    def _housekeeping(self):
        now = time.time()
        if not self._enabled():
            if self._samples:
                self._samples.clear()
            with self._lock:
                self.telemetry["current"] = None
            return
        # Verify the device's current channel before treating a segment-fetch
        # gap as a channel change. Players buffer well ahead of the decoder,
        # so short gaps are normal and must not end a 60-second sample.
        if now - self._probe_at >= PROBE_INTERVAL_SECONDS:
            current = self._probe()
            self._probe_at = now
            self._probe_cid = current.get("channelID") if isinstance(current, dict) else None
        verified = self._probe_cid if now - self._probe_at <= PROBE_INTERVAL_SECONDS * 3 else None
        for cid, sample in list(self._samples.items()):
            best = self._best_base(sample)
            if not best:
                if now - sample.last_tap >= IDLE_SECONDS:
                    self._finalize(cid, "idle")
                continue
            if best.seconds >= TARGET_SECONDS:
                self._finalize(cid, "target_reached")
            elif verified and verified != cid and best.seconds >= MIN_SAMPLE_SECONDS:
                self._finalize(cid, "channel_changed")
            elif verified and verified == cid:
                # Still the current channel: keep accumulating across buffer gaps.
                continue
            elif now - sample.last_tap >= IDLE_SECONDS:
                self._finalize(cid, "idle")
        self._update_current()

    def _best_base(self, sample):
        candidates = list(sample.bases.values())
        if not candidates:
            return None
        audio = [base for base in candidates if base.is_audio and base.seconds >= MIN_SAMPLE_SECONDS]
        pool = audio or candidates
        return max(pool, key=lambda base: (base.seconds, base.is_audio))

    def _update_current(self):
        if not self._samples:
            with self._lock:
                self.telemetry["current"] = None
            self._persist()
            return
        cid, sample = max(self._samples.items(), key=lambda item: item[1].last_tap)
        best = self._best_base(sample)
        with self._lock:
            self.telemetry["current"] = {
                "channelID": cid, "providerID": sample.provider, "name": sample.name,
                "startedAt": round(sample.started, 3), "seconds": round(best.seconds if best else 0.0, 2),
                "targetSeconds": TARGET_SECONDS,
                "windows": min(MAX_WINDOWS, int((best.seconds if best else 0.0) // WINDOW_SECONDS)),
                "segmentsDecoded": sum(base.segments for base in sample.bases.values()),
                "decodeFailures": sum(base.failures for base in sample.bases.values()),
                "lastSegmentAt": round(sample.last_tap, 3),
            }
        self._persist()

    def _finalize(self, cid, reason):
        sample = self._samples.pop(cid, None)
        if sample is None:
            return
        self._finalized[cid] = time.time()
        if len(self._finalized) > 256:
            cutoff = time.time() - FINALIZE_COOLDOWN_SECONDS
            for key, value in list(self._finalized.items()):
                if value < cutoff:
                    self._finalized.pop(key, None)
        best = self._best_base(sample)
        if best is not None and best.seconds < MIN_REPORT_SECONDS:
            return
        if best is None or best.seconds < MIN_SAMPLE_SECONDS:
            self._record_last(cid, sample.provider, sample.name, {
                "outcome": "inconclusive", "reason": "sample_too_short", "confidence": 0.0,
                "speechSeconds": 0.0, "sampleSeconds": round(best.seconds if best else 0.0, 2),
                "speechCoverage": 0.0, "windows": 0, "languages": [], "lexicalMargin": None,
            }, device_verified=None, changed=False, marked=False, reason_detail=reason)
            return
        if not self._enabled():
            return
        end_probe = self._probe()
        if end_probe and end_probe.get("channelID"):
            self._probe_cid = end_probe["channelID"]
            self._probe_at = time.time()
        start_verified = bool(sample.probe and sample.probe.get("channelID") == cid)
        identity_conflict = bool(sample.probe and sample.probe.get("channelID") and sample.probe.get("channelID") != cid)
        changed = bool(end_probe and end_probe.get("channelID") and end_probe.get("channelID") != cid)
        if identity_conflict:
            self._record_last(cid, sample.provider, sample.name, {
                "outcome": "inconclusive", "reason": "session_mismatch", "confidence": 0.0,
                "speechSeconds": 0.0, "sampleSeconds": round(best.seconds, 2),
                "speechCoverage": 0.0, "windows": 0, "languages": [], "lexicalMargin": None,
            }, device_verified=False, changed=changed, marked=False, reason_detail=reason)
            return
        job = {
            "cid": cid, "provider": sample.provider, "name": sample.name,
            "pcm": bytes(best.pcm), "seconds": best.seconds,
            "deviceVerified": start_verified if sample.probe else None,
            "changed": changed, "finalizeReason": reason,
        }
        try:
            self._jobs.put_nowait(job)
        except Exception:
            with self._lock:
                self.telemetry["counters"]["errors"] += 1
        self._update_current()

    def _infer_loop(self):
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except Exception:
                continue
            if job is None:
                continue
            try:
                self._infer(job)
            except Exception as error:
                self.log("spanish auto inference error " + type(error).__name__)
                with self._lock:
                    self.telemetry["counters"]["errors"] += 1
                    self.telemetry["counters"]["inconclusive"] += 1
                    self.telemetry["counters"]["tested"] += 1
                self._persist(force=True)

    def _infer(self, job):
        if not self._enabled():
            return
        workdir = tempfile.mkdtemp(prefix="job-", dir=self._tmpdir)
        try:
            analysis = analyze_speech(job["pcm"])
            if analysis["duration"] < MIN_SAMPLE_SECONDS or analysis["speechSeconds"] < MIN_SPEECH_SECONDS:
                decision = _decision(
                    "inconclusive", "sample_too_short" if analysis["duration"] < MIN_SAMPLE_SECONDS else "insufficient_speech",
                    0.0, analysis, [], None,
                )
            else:
                window_results = []
                for window in analysis["windows"]:
                    if not window["usable"]:
                        continue
                    start = int(window["start"] * RATE) * WIDTH
                    end = int((window["start"] + window["duration"]) * RATE) * WIDTH
                    path = Path(workdir) / ("w%d.wav" % window["index"])
                    _write_wav(path, job["pcm"][start:end])
                    detected = detect_language(path, self.whisper, self.model_path, self.threads)
                    if detected:
                        window_results.append({
                            "index": window["index"], "start": window["start"],
                            "duration": window["duration"], "speechSeconds": window["speechSeconds"],
                            "speechRatio": window["speechRatio"], "usable": True,
                            "language": detected[0], "probability": detected[1],
                        })
                full = Path(workdir) / "full.wav"
                _write_wav(full, job["pcm"])
                transcript = transcribe(full, self.whisper, self.model_path, workdir, self.threads)
                lexical = lexical_margin(transcript[1]) if transcript else (None, 0, 0)
                if transcript and transcript[0] not in ("es", "en", "pt"):
                    lexical = (None, lexical[1], lexical[2])
                decision = decide(analysis, window_results, lexical)
            self._record_result(job, decision)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _record_result(self, job, decision):
        outcome = decision["outcome"]
        reason = decision["reason"]
        marked = False
        suppressed = False
        commit_reason = None
        if outcome == "spanish" and self._enabled():
            evidence = {
                "model": Path(self.model_path).name, "windows": decision.get("windows", 0),
                "speechSeconds": decision.get("speechSeconds"), "sampleSeconds": decision.get("sampleSeconds"),
                "speechCoverage": decision.get("speechCoverage"), "languages": decision.get("languages", []),
                "deviceVerified": job.get("deviceVerified"), "channelChangedBeforeResult": bool(job.get("changed")),
                "lexicalMargin": decision.get("lexicalMargin"),
            }
            try:
                result = self.on_spanish(
                    job["cid"], job["provider"], job["name"] or job["provider"],
                    decision.get("confidence", 0.0), "auto:spanish_dominant", evidence,
                )
                commit_reason = (result or {}).get("reason")
                if (result or {}).get("changed"):
                    marked = True
                elif commit_reason == "manual_not_spanish":
                    suppressed = True
                    outcome = "inconclusive"
                    reason = "manual_not_spanish"
                elif commit_reason in ("manual_spanish", "already_marked"):
                    marked = True
                elif commit_reason == "unknown_provider":
                    outcome = "inconclusive"
                    reason = "unknown_provider"
            except Exception as error:
                commit_reason = "commit_" + type(error).__name__
                outcome = "inconclusive"
                reason = "commit_error"
        with self._lock:
            counters = self.telemetry["counters"]
            counters["tested"] += 1
            counters["samplesFinalized"] += 1
            if outcome == "spanish":
                counters["spanish"] += 1
            elif outcome == "non_spanish":
                counters["nonSpanish"] += 1
            else:
                counters["inconclusive"] += 1
            if suppressed:
                counters["suppressed"] += 1
            if reason in ("commit_error", "inference_error"):
                counters["errors"] += 1
            last = {
                "at": round(time.time(), 3), "channelID": job["cid"], "providerID": job["provider"],
                "name": job["name"], "outcome": outcome, "reason": reason,
                "confidence": decision.get("confidence", 0.0),
                "speechSeconds": decision.get("speechSeconds", 0.0),
                "sampleSeconds": decision.get("sampleSeconds", 0.0),
                "speechCoverage": decision.get("speechCoverage", 0.0),
                "windows": decision.get("windows", 0),
                "deviceVerified": job.get("deviceVerified"),
                "channelChangedBeforeResult": bool(job.get("changed")),
                "marked": marked, "suppressed": suppressed, "commitReason": commit_reason,
            }
            self.telemetry["last"] = last
            self.telemetry["current"] = None
        if marked:
            self.log("SPANISH_AUTO_MARK provider=%s channel=%s confidence=%.3f speech=%.1fs marked=%s" % (
                job["provider"], job["cid"], decision.get("confidence", 0.0), decision.get("speechSeconds", 0.0), marked))
        else:
            self.log("SPANISH_AUTO_RESULT provider=%s channel=%s outcome=%s reason=%s confidence=%.3f speech=%.1fs" % (
                job["provider"], job["cid"], outcome, reason, decision.get("confidence", 0.0), decision.get("speechSeconds", 0.0)))
        self._persist(force=True)

    def _record_last(self, cid, provider, name, decision, device_verified, changed, marked, reason_detail):
        with self._lock:
            counters = self.telemetry["counters"]
            counters["tested"] += 1
            counters["samplesFinalized"] += 1
            counters["inconclusive"] += 1
            self.telemetry["last"] = {
                "at": round(time.time(), 3), "channelID": cid, "providerID": provider, "name": name,
                "outcome": decision.get("outcome", "inconclusive"),
                "reason": decision.get("reason", reason_detail),
                "confidence": decision.get("confidence", 0.0),
                "speechSeconds": decision.get("speechSeconds", 0.0),
                "sampleSeconds": decision.get("sampleSeconds", 0.0),
                "speechCoverage": decision.get("speechCoverage", 0.0),
                "windows": decision.get("windows", 0),
                "deviceVerified": device_verified,
                "channelChangedBeforeResult": bool(changed),
                "marked": bool(marked), "suppressed": False, "commitReason": None,
            }
            self.telemetry["current"] = None
        self._persist(force=True)

    def status(self):
        now = time.time()
        with self._lock:
            counters = dict(self.telemetry["counters"])
            last = self.telemetry.get("last")
            current = self.telemetry.get("current")
            last_media = self._last_media
        active = bool(not self.fault and self._enabled() and last_media and now - last_media <= INACTIVE_SECONDS)
        decisions = counters.get("spanish", 0) + counters.get("nonSpanish", 0) + counters.get("inconclusive", 0)
        return {
            "revision": REVISION,
            "enabled": bool(not self.fault and self._enabled()),
            "active": active,
            "fault": self.fault,
            "model": Path(self.model_path).name,
            "thresholds": {
                "sampleSeconds": TARGET_SECONDS, "minSampleSeconds": MIN_SAMPLE_SECONDS,
                "minSpeechSeconds": MIN_SPEECH_SECONDS, "minCoverage": MIN_COVERAGE,
                "minWindows": MIN_WINDOWS, "minWindowProbability": MIN_WINDOW_PROBABILITY,
                "minSpanishProbability": MIN_SPANISH_PROBABILITY,
            },
            "counters": counters,
            "decisionTotal": decisions,
            "pending": current is not None or bool(self._jobs and self._jobs.qsize()),
            "current": current,
            "last": last,
            "diagnostic": self.telemetry.get("diagnostic"),
            "lastMediaAt": round(last_media, 3) if last_media else None,
        }
