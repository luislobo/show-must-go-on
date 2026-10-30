# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = ["kokoro-onnx==0.6.1", "soundfile==0.14.0"]
# ///
"""Voice the demo's narration lines with Kokoro, on this machine.

    uv run demo/voice.py <lines.json> <out-dir>

<lines.json> is what `demo/record.py --lines` prints: each line's caption
text and the text to speak. Writes one WAV per line to <out-dir> and
voice.json, listing each clip with its caption and length, which
demo/record.py reads to pace the recording.

Kokoro v1.0 (82M parameters, Apache 2.0) runs locally through kokoro-onnx;
nothing is sent anywhere. Its two model files are read from
${KOKORO_HOME:-~/.local/share/kokoro} (see "Demo video" in the README for
where to get them). SMGO_VOICE and SMGO_SPEED pick the voice (default
af_heart) and speed (default 1.1). Python stays below 3.15 because
onnxruntime has no wheels for it yet.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

MODELS = Path(os.environ.get("KOKORO_HOME", Path.home() / ".local/share/kokoro"))
FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def main(lines_path: Path, out: Path) -> None:
    for name in FILES:
        if not (MODELS / name).exists():
            raise SystemExit(f"{MODELS / name}: missing; see 'Demo video' in README.md")
    voice = os.environ.get("SMGO_VOICE", "af_heart")
    speed = float(os.environ.get("SMGO_SPEED", "1.1"))
    lang = "en-gb" if voice.startswith("b") else "en-us"
    kokoro = Kokoro(str(MODELS / FILES[0]), str(MODELS / FILES[1]))

    out.mkdir(parents=True, exist_ok=True)
    clips = []
    for n, line in enumerate(json.loads(lines_path.read_text()), 1):
        samples, rate = kokoro.create(line["spoken"], voice=voice, speed=speed, lang=lang)
        clip = out / f"line-{n:02d}.wav"
        sf.write(clip, samples, rate)
        clips.append({"file": clip.name, "text": line["text"],
                      "seconds": round(len(samples) / rate, 3)})
    # written last, so a failed run leaves no voice.json for record.py to trust
    (out / "voice.json").write_text(json.dumps(
        {"voice": voice, "speed": speed, "lines": clips}, indent=1))
    total = sum(c["seconds"] for c in clips)
    print(f"voiced {len(clips)} lines with {voice} at {speed}: {total:.0f} s of speech")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
