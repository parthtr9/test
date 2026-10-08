from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from PIL import Image

import app.main as main


async def fake_metadata(source_url: str):
    return {
        "source_url": source_url,
        "author": "Smoke Tester",
        "published_at": datetime(2026, 8, 19, tzinfo=timezone.utc),
        "site_name": "Example News",
    }


def make_image(path: Path) -> None:
    image = Image.new("RGB", (640, 360), "#477e9f")
    image.save(path)


def make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=24",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def submit_file(client: TestClient, path: Path, content_type: str) -> str:
    with path.open("rb") as file:
        response = client.post(
            "/submit",
            data={"source_url": "https://example.com/source-article"},
            files={"media_file": (path.name, file, content_type)},
        )
    assert response.status_code == 200, response.text
    assert "Marked file ready" in response.text
    marker = "/download/"
    assert marker in response.text
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def main_smoke() -> None:
    main.extract_metadata = fake_metadata
    client = TestClient(main.app)

    tmp = Path("data/smoke")
    tmp.mkdir(parents=True, exist_ok=True)

    image_path = tmp / "sample.jpg"
    video_path = tmp / "sample.mp4"
    make_image(image_path)
    make_video(video_path)

    image_code = submit_file(client, image_path, "image/jpeg")
    video_code = submit_file(client, video_path, "video/mp4")

    for code in (image_code, video_code):
        response = client.get(f"/r/{code}")
        assert response.status_code == 200
        assert "Example News" in response.text
        response = client.get(f"/download/{code}")
        assert response.status_code == 200

    assert not list(main.DATA_DIR.glob("original*"))
    print("smoke ok")


if __name__ == "__main__":
    main_smoke()
