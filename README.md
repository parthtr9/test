# Media Provenance MVP

A small FastAPI app that records source metadata, creates a QR resolver marker,
burns it into uploaded image/video media, and returns the marked file.

The app deliberately does **not** store or serve full original media. It stores:

- metadata
- SHA-256 hash of the original upload
- thumbnail only
- generated QR marker
- generated marked output for immediate user download through an explicit route

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

`ffmpeg` must be installed for video marking.

## Usage

Submit a source article URL and an image/video file. The app extracts article
metadata, stores the provenance record, burns a standard QR code into the media,
and returns a download link for the marked file. The QR resolves to:

```text
/r/<short_code>
```
