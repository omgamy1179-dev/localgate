"""Local-only OCR. No cloud calls, ever.

Engine chain (mode=auto):
1. tesseract binary, if present
2. macOS Vision framework via a small Swift helper compiled once and cached in
   the service data dir (compiled with the system swiftc)
3. unavailable -> OCR is skipped; the failure is counted in metrics, never fatal

In off mode OCR is disabled outright.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

VISION_SWIFT = r"""
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else { exit(2) }
let url = URL(fileURLWithPath: args[1])
guard let img = NSImage(contentsOf: url),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(3)
}
let request = VNRecognizeTextRequest { req, _ in
    let lines = (req.results as? [VNRecognizedTextObservation])?.compactMap { obs in
        obs.topCandidates(1).first?.string
    } ?? []
    print(lines.joined(separator: "\n"))
}
let langs = ProcessInfo.processInfo.environment["LG_OCR_LANGS"]
    .map { $0.split(separator: ",").map(String.init) } ?? ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
request.recognitionLanguages = langs
if #available(macOS 13.0, *) {
    request.usesLanguageCorrection = false
}
do {
    try VNImageRequestHandler(cgImage: cg).perform([request])
} catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(4)
}
"""


class OcrUnavailable(Exception):
    pass


class OcrError(Exception):
    pass


# BCP-47 (config / macOS Vision style) -> tesseract language codes.
# Vision accepts BCP-47 directly; tesseract needs its own codes, so the two
# engines must never share the raw configured list.
_TESSERACT_LANG_MAP = {
    "zh": "chi_sim", "zh-hans": "chi_sim", "zh-cn": "chi_sim", "zh-sg": "chi_sim",
    "zh-hant": "chi_tra", "zh-tw": "chi_tra", "zh-hk": "chi_tra", "zh-mo": "chi_tra",
    "en": "eng", "en-us": "eng", "en-gb": "eng", "en-au": "eng", "en-ca": "eng",
    "ja": "jpn", "ko": "kor", "fr": "fra", "fr-ca": "fra", "de": "deu",
    "es": "spa", "it": "ita", "pt": "por", "pt-br": "por", "ru": "rus",
    "ar": "ara", "hi": "hin", "th": "tha", "vi": "vie", "tr": "tur",
    "nl": "nld", "pl": "pol", "sv": "swe", "da": "dan", "fi": "fin",
    "no": "nor", "nb": "nor", "nn": "nor", "cs": "ces", "el": "ell",
    "he": "heb", "hu": "hun", "id": "ind", "ro": "ron", "uk": "ukr",
}


def tesseract_langs(languages: list[str] | None) -> str:
    """Map BCP-47 language tags to a '+ '-joined tesseract language string.

    Unmappable tags are dropped; if nothing maps, 'eng' is used so the call
    always requests at least one installed language."""
    out: list[str] = []
    for tag in languages or []:
        t = (tag or "").strip().lower().replace("_", "-")
        if not t:
            continue
        code = _TESSERACT_LANG_MAP.get(t)
        if code is None and "-" in t:
            code = _TESSERACT_LANG_MAP.get(t.split("-", 1)[0])
        if code and code not in out:
            out.append(code)
    return "+".join(out) if out else "eng"


def _ocr_env() -> dict[str, str]:
    """Explicit allowlist environment for OCR child processes (no inherited
    miscellany, no DYLD*/LD_* injection vectors)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "OMP_THREAD_LIMIT": "1",  # keep OCR from spawning thread farms
        "LG_OCR_LANGS": "",
    }
    return {k: v for k, v in env.items() if v != "" or k == "LG_OCR_LANGS"}


class OcrEngine:
    def __init__(self, mode: str = "auto", languages: list[str] | None = None,
                 timeout_s: int = 60, helper_cache_dir: str | None = None):
        self.mode = mode
        self.languages = languages or ["zh-Hans", "en-US"]
        self.timeout_s = max(5, int(timeout_s))
        self._helper_dir = helper_cache_dir
        self._helper_path: str | None = None
        self._helper_lock = threading.Lock()
        self._probe_started = threading.Event()
        self._tesseract: str | None | bool = None  # lazily probed
        self._vision_broken = False

    # -- engines ---------------------------------------------------------

    def _tesseract_bin(self) -> str | None:
        if self._tesseract is None:
            self._tesseract = shutil.which("tesseract")
        return self._tesseract  # type: ignore[return-value]

    def _vision_helper(self) -> str | None:
        if self._vision_broken:
            return None
        with self._helper_lock:
            if self._helper_path:
                return self._helper_path
            cache_dir = self._helper_dir or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bin")
            os.makedirs(cache_dir, exist_ok=True)
            helper = os.path.join(cache_dir, "localgate-ocr-helper")
            if os.path.exists(helper) and os.access(helper, os.X_OK):
                self._helper_path = helper
                return helper
            if not shutil.which("swiftc"):
                self._vision_broken = True
                return None
            src = os.path.join(cache_dir, "localgate_ocr_helper.swift")
            try:
                with open(src, "w", encoding="utf-8") as f:
                    f.write(VISION_SWIFT)
                proc = subprocess.run(
                    ["swiftc", "-O", src, "-o", helper],
                    capture_output=True, timeout=180)
                if proc.returncode != 0 or not os.path.exists(helper):
                    self._vision_broken = True
                    return None
                self._helper_path = helper
                return helper
            except (OSError, subprocess.TimeoutExpired):
                self._vision_broken = True
                return None

    # -- public ----------------------------------------------------------

    def available(self) -> str | None:
        """Non-blocking engine probe for status endpoints.

        Returns the cached engine once known ("tesseract"/"vision"), or None
        while the first probe is still running in the background. Detecting
        the Vision engine may compile a Swift helper (potentially minutes on
        a cold toolchain) - that must never block an HTTP handler or the
        self-check loop, so the first probe is asynchronous."""
        if self.mode == "off":
            return None
        # plain attribute reads: the background probe holds _helper_lock for
        # the whole (potentially minutes-long) helper compilation, so this
        # path must never contend with it. A slightly stale read is fine -
        # the probe updates the flags when it finishes.
        if self._tesseract_bin():
            return "tesseract"
        if self._vision_broken:
            return None
        if self._helper_path:
            return "vision"
        self._start_background_probe()
        return None

    def available_blocking(self) -> str | None:
        """Full probe; may compile the Vision helper (slow on first run)."""
        if self.mode == "off":
            return None
        if self._tesseract_bin():
            return "tesseract"
        if self._vision_helper():
            return "vision"
        return None

    def _start_background_probe(self) -> None:
        if self._probe_started.is_set():
            return
        self._probe_started.set()
        threading.Thread(target=self._background_probe,
                         name="localgate-ocr-probe", daemon=True).start()

    def _background_probe(self) -> None:
        try:
            self.available_blocking()
        except Exception:
            pass

    def recognize(self, image_path: str) -> tuple[str, str]:
        """Returns (text, engine). Raises OcrUnavailable / OcrError."""
        if self.mode == "off":
            raise OcrUnavailable("ocr disabled by config")
        engine = None
        if self._tesseract_bin():
            engine = "tesseract"
        else:
            helper = self._vision_helper()
            if helper:
                engine = "vision"
        if engine is None:
            raise OcrUnavailable("no local ocr engine available")
        try:
            if engine == "tesseract":
                langs = tesseract_langs(self.languages)
                proc = subprocess.run(
                    ["tesseract", image_path, "stdout", "-l", langs],
                    capture_output=True, timeout=self.timeout_s,
                    env=_ocr_env())
                # the mapped languages may not be installed locally: fall back
                # to the default once instead of failing the file outright
                if proc.returncode != 0 and b"Failed loading language" in (proc.stderr or b""):
                    proc = subprocess.run(
                        ["tesseract", image_path, "stdout"],
                        capture_output=True, timeout=self.timeout_s,
                        env=_ocr_env())
            else:
                helper = self._vision_helper()
                if helper is None:  # pragma: no cover - checked above
                    raise OcrUnavailable("no local ocr engine available")
                env = _ocr_env()
                # Vision reads BCP-47 tags natively
                env["LG_OCR_LANGS"] = ",".join(self.languages)
                proc = subprocess.run([helper, image_path],
                                      capture_output=True, timeout=self.timeout_s,
                                      env=env)
        except subprocess.TimeoutExpired as e:
            raise OcrError(f"ocr timeout after {self.timeout_s}s") from e
        except OSError as e:
            raise OcrError(f"ocr spawn failed: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[:200]
            raise OcrError(f"ocr exit {proc.returncode}: {detail}")
        return proc.stdout.decode("utf-8", errors="replace").strip(), engine
