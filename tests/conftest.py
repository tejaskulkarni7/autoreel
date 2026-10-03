import os
import subprocess
from pathlib import Path

import pytest

os.environ["AUTOREEL_DISABLE_WORKERS"] = "1"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets a fresh data dir + database."""
    from autoreel import config, db

    monkeypatch.setenv("AUTOREEL_DATA_DIR", str(tmp_path / "data"))
    config.get_server_settings.cache_clear()
    db.reset_engine()
    yield tmp_path / "data"
    db.reset_engine()
    config.get_server_settings.cache_clear()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """A 12s 640x360 video with a moving bright 'subject' and a tone."""
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True,
    )
    return out


def fake_transcript(duration: float = 12.0) -> dict:
    sentences = [
        "So here is the thing nobody tells you.",
        "You have to start before you are ready.",
        "I waited three years and that was a huge mistake.",
        "Honestly it was the worst decision of my life.",
        "Do not make the same mistake I did.",
        "Start today and thank me later.",
    ]
    segments, t = [], 0.0
    per_word = duration / sum(len(s.split()) for s in sentences)
    for s in sentences:
        words = []
        for w in s.split():
            words.append({"start": round(t, 3), "end": round(t + per_word * 0.9, 3), "word": " " + w, "p": 0.9})
            t += per_word
        segments.append({"start": words[0]["start"], "end": words[-1]["end"], "text": s, "words": words})
    return {"language": "en", "duration": duration, "segments": segments}


class FakeLLM:
    def __init__(self, clips):
        self.clips = clips
        self.calls = []

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        return {"clips": self.clips}
