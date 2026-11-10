"""Mix the narration clips from demo/record.py into one audio track, with subtitles.

    python3 demo/narrate.py <narration-dir> <out.m4a> <out.srt>

<narration-dir>/narration.json lists each clip with its start time in the
cast. Each clip is delayed to its start, the clips are mixed and
normalised to -16 LUFS into an AAC track, and an SRT file gets one
caption per line, on screen while it is spoken. Needs ffmpeg.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main(folder: Path, audio: Path, captions: Path) -> None:
    clips = json.loads((folder / "narration.json").read_text())
    if not clips:
        raise SystemExit(f"{folder / 'narration.json'}: no narration lines")
    inputs: list[str] = []
    chains: list[str] = []
    for n, clip in enumerate(clips):
        inputs += ["-i", str(folder / clip["file"])]
        delay = round(clip["start"] * 1000)
        chains.append(f"[{n}]adelay={delay}:all=1[a{n}]")
    mix = "".join(f"[a{n}]" for n in range(len(clips)))
    graph = (";".join(chains) + f";{mix}amix=inputs={len(clips)}:normalize=0,"
             "loudnorm=I=-16:TP=-1.5:LRA=11[out]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph,
                    "-map", "[out]", "-c:a", "aac", "-b:a", "128k", str(audio)], check=True)

    with captions.open("w") as f:
        for n, clip in enumerate(clips, 1):
            end = clip["start"] + clip["seconds"] + 0.3
            f.write(f"{n}\n{srt_time(clip['start'])} --> {srt_time(end)}\n{clip['text']}\n\n")
    print(f"narration: {len(clips)} lines, {audio.name} and {captions.name}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
