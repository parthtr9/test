from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import qrcode
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from readability import Document
from sqlalchemy import DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ASSET_DIR = DATA_DIR / "assets"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"
QR_DIR = DATA_DIR / "qr"
OUTPUT_DIR = DATA_DIR / "marked"
DB_PATH = DATA_DIR / "app.sqlite3"

for directory in (ASSET_DIR, THUMBNAIL_DIR, QR_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

app = FastAPI(title="Media Provenance MVP")
app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")
app.mount("/qr", StaticFiles(directory=QR_DIR), name="qr")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    short_code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    author: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(512), nullable=True)


Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def app_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def random_short_code(db: Session) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(7))
        if not db.scalar(select(Record).where(Record.short_code == code)):
            return code


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def first_meta(soup: BeautifulSoup, selectors: list[tuple[str, str]]) -> str | None:
    for attr, value in selectors:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


async def extract_metadata(source_url: str) -> dict[str, str | datetime | None]:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Source URL must be an http(s) URL.")

    fallback = {
        "source_url": source_url,
        "author": None,
        "published_at": None,
        "site_name": parsed.netloc.removeprefix("www."),
    }

    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "MediaProvenanceMVP/0.1"},
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return fallback

    soup = BeautifulSoup(response.text, "html.parser")
    document = Document(response.text)
    site_name = first_meta(
        soup,
        [
            ("property", "og:site_name"),
            ("name", "application-name"),
            ("name", "twitter:site"),
        ],
    )
    if not site_name:
        site_name = parsed.netloc.removeprefix("www.")
    if not site_name and document.short_title():
        site_name = document.short_title()

    author = first_meta(
        soup,
        [
            ("name", "author"),
            ("property", "article:author"),
            ("name", "byl"),
            ("name", "parsely-author"),
        ],
    )

    published_raw = first_meta(
        soup,
        [
            ("property", "article:published_time"),
            ("name", "article:published_time"),
            ("name", "pubdate"),
            ("name", "publishdate"),
            ("name", "date"),
            ("itemprop", "datePublished"),
        ],
    )

    return {
        "source_url": str(response.url),
        "author": author,
        "published_at": parse_datetime(published_raw),
        "site_name": site_name,
    }


def copy_and_hash_upload(upload: UploadFile, destination: Path) -> str:
    sha256 = hashlib.sha256()
    with destination.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            sha256.update(chunk)
            out.write(chunk)
    upload.file.seek(0)
    return sha256.hexdigest()


def file_kind(content_type: str | None, filename: str) -> str:
    guessed = content_type or mimetypes.guess_type(filename)[0] or ""
    if guessed.startswith("image/"):
        return "image"
    if guessed.startswith("video/"):
        return "video"
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"}:
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
        return "video"
    raise HTTPException(status_code=400, detail="Upload must be an image or video file.")


def make_qr(resolver_url: str, short_code: str) -> Path:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(resolver_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    path = QR_DIR / f"{short_code}.png"
    image.save(path)
    return path


def create_image_thumbnail(source_path: Path, short_code: str) -> Path:
    thumb_path = THUMBNAIL_DIR / f"{short_code}.jpg"
    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((480, 480))
        image.save(thumb_path, "JPEG", quality=82)
    return thumb_path


def run_ffmpeg(args: list[str]) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="ffmpeg is required for video processing.") from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or "ffmpeg failed"
        raise HTTPException(status_code=500, detail=details[-1200:]) from exc


def create_video_thumbnail(source_path: Path, short_code: str) -> Path:
    thumb_path = THUMBNAIL_DIR / f"{short_code}.png"
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "0",
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(480,iw)':-2",
            str(thumb_path),
        ]
    )
    return thumb_path


def overlay_qr_on_image(source_path: Path, qr_path: Path, short_code: str) -> Path:
    output_path = OUTPUT_DIR / f"{short_code}{source_path.suffix.lower() or '.png'}"
    with Image.open(source_path) as base:
        base = ImageOps.exif_transpose(base).convert("RGBA")
        qr = Image.open(qr_path).convert("RGBA")
        qr_size = max(96, min(base.width, base.height) // 5)
        qr.thumbnail((qr_size, qr_size), Image.Resampling.LANCZOS)
        pad = max(12, min(base.width, base.height) // 40)
        plate = Image.new("RGBA", (qr.width + pad, qr.height + pad), "white")
        plate.alpha_composite(qr, (pad // 2, pad // 2))
        position = (base.width - plate.width - pad, base.height - plate.height - pad)
        base.alpha_composite(plate, position)
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            base.convert("RGB").save(output_path, quality=92)
        else:
            base.save(output_path)
    return output_path


def overlay_qr_on_video(source_path: Path, qr_path: Path, short_code: str) -> Path:
    output_path = OUTPUT_DIR / f"{short_code}.mp4"
    filter_graph = (
        "[1:v][0:v]scale2ref=w='max(96,min(main_w,main_h)/6)':h=-1[qr][base];"
        "[base][qr]overlay=x=main_w-overlay_w-24:y=main_h-overlay_h-24:format=auto[outv]"
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-loop",
            "1",
            "-i",
            str(qr_path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[outv]",
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )
    return output_path


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/submit", response_class=HTMLResponse)
async def submit(
    request: Request,
    source_url: Annotated[str, Form()],
    media_file: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
):
    if not media_file.filename:
        raise HTTPException(status_code=400, detail="Upload a media file to mark.")

    kind = file_kind(media_file.content_type, media_file.filename)
    metadata = await extract_metadata(source_url.strip())
    short_code = random_short_code(db)
    resolver_url = f"{app_base_url(request)}/r/{short_code}"
    qr_path = make_qr(resolver_url, short_code)

    with tempfile.TemporaryDirectory(prefix="media-provenance-") as tmp:
        suffix = Path(media_file.filename).suffix.lower()
        original_path = Path(tmp) / f"original{suffix}"
        content_hash = copy_and_hash_upload(media_file, original_path)

        if kind == "image":
            thumbnail_path = create_image_thumbnail(original_path, short_code)
            marked_path = overlay_qr_on_image(original_path, qr_path, short_code)
        else:
            thumbnail_path = create_video_thumbnail(original_path, short_code)
            marked_path = overlay_qr_on_video(original_path, qr_path, short_code)

    record = Record(
        short_code=short_code,
        source_url=str(metadata["source_url"]),
        author=metadata["author"] or None,
        published_at=metadata["published_at"],
        saved_at=datetime.now(timezone.utc),
        content_hash=content_hash,
        thumbnail_path=thumbnail_path.name,
        site_name=metadata["site_name"] or None,
    )
    db.add(record)
    db.commit()

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "record": record,
            "resolver_url": resolver_url,
            "download_url": f"/download/{short_code}",
            "qr_url": f"/qr/{short_code}.png",
        },
    )


@app.get("/download/{short_code}")
async def download_marked(short_code: str, db: Annotated[Session, Depends(get_db)]):
    record = db.scalar(select(Record).where(Record.short_code == short_code))
    if not record:
        raise HTTPException(status_code=404, detail="Record not found.")
    matches = list(OUTPUT_DIR.glob(f"{short_code}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Marked file not found.")
    return FileResponse(matches[0], filename=matches[0].name)


@app.get("/r/{short_code}", response_class=HTMLResponse)
async def resolve(request: Request, short_code: str, db: Annotated[Session, Depends(get_db)]):
    record = db.scalar(select(Record).where(Record.short_code == short_code))
    if not record:
        raise HTTPException(status_code=404, detail="Record not found.")
    return templates.TemplateResponse("resolver.html", {"request": request, "record": record})


@app.get("/health")
async def health():
    return {"ok": True}
