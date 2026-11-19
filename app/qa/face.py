"""Face detection and matching (guide 11.1): OpenCV YuNet (detector) and SFace (recogniser).

face_vector(image)   -> 128 floats for the largest detected face, or None
similarity(a, b)     -> cosine similarity
Model files live in models/ (see docs/VENDOR_NOTES.md for licences).
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

MODELS = Path(__file__).resolve().parent.parent.parent / "models"
DETECTOR_FILE = MODELS / "face_detection_yunet_2026may.onnx"
RECOGNIZER_FILE = MODELS / "face_recognition_sface_2021dec.onnx"
SCORE_THRESHOLD = 0.7
MAX_DETECT_SIDE = 1280  # large photos are shrunk before detection


@lru_cache(maxsize=1)
def _models():
    import cv2

    detector = cv2.FaceDetectorYN.create(str(DETECTOR_FILE), "", (320, 320), SCORE_THRESHOLD)
    recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_FILE), "")
    return detector, recognizer


def _decode(image: bytes | np.ndarray):
    import cv2

    if isinstance(image, np.ndarray):
        return image
    arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("the image could not be decoded")
    return arr


def detect_faces(image: bytes | np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Returns (image used for detection, detections as an N x 15 array; empty when none)."""
    import cv2

    img = _decode(image)
    h, w = img.shape[:2]
    scale = min(1.0, MAX_DETECT_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    detector, _ = _models()
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    return img, (faces if faces is not None else np.empty((0, 15), dtype=np.float32))


def count_faces(image: bytes | np.ndarray) -> int:
    return len(detect_faces(image)[1])


def face_vector(image: bytes | np.ndarray) -> list[float] | None:
    """128 floats for the largest face (by box area), or None when no face is found."""
    img, faces = detect_faces(image)
    if len(faces) == 0:
        return None
    largest = max(faces, key=lambda f: f[2] * f[3])
    _, recognizer = _models()
    feature = recognizer.feature(recognizer.alignCrop(img, largest))
    return [float(v) for v in np.asarray(feature).reshape(-1)]


def similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two face vectors."""
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(va @ vb) / denom if denom else 0.0


def stub_vector(seed: str) -> list[float]:
    """Deterministic unit vector for dry-run fixtures: different seeds give unrelated vectors."""
    out: list[float] = []
    counter = 0
    while len(out) < 128:
        out.extend((b - 127.5) / 127.5 for b in hashlib.sha256(f"{counter}|{seed}".encode()).digest())
        counter += 1
    out = out[:128]
    norm = math.sqrt(sum(v * v for v in out))
    return [v / norm for v in out]
