#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Camera backend for the phone screen view.

A background thread keeps draining the capture device so ``frame()`` can
always return a frame that STARTED being read after the request.  That
matters here: the machine parks the pen away, the caller asks for a picture,
and any stale buffered frame would show the screen before the touch.

The default backend is any OpenCV capture device.  When e2eSoft iVCam (or a
phone-as-webcam app) is installed it is preferred by name, so a nearby OBS
virtual camera cannot be mistaken for the phone feed.  ``pygrabber`` is
optional; without it the configured index is tried first.
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

import cv2
import numpy as np


class VisionError(RuntimeError):
    pass


def encode_jpeg(image: np.ndarray, quality: int = 82) -> bytes:
    ok, encoded = cv2.imencode(
        ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(np.clip(quality, 40, 95))]
    )
    if not ok:
        raise VisionError("Could not encode camera preview")
    return encoded.tobytes()


def resize_long_edge(image: np.ndarray, max_edge: int = 1280) -> np.ndarray:
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return image.copy()
    scale = max_edge / longest
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _device_names() -> list[str]:
    try:
        from pygrabber.dshow_graph import FilterGraph

        return FilterGraph().get_input_devices()
    except Exception:
        return []


def _looks_alive(sample: np.ndarray) -> bool:
    """Reject splash screens: a real desk scene has colour variety and contrast."""
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    quantised = (cv2.resize(sample, (160, 90)) // 32).reshape(-1, 3)
    colour_count = int(np.unique(quantised, axis=0).shape[0])
    return (
        8.0 <= float(gray.mean()) <= 215.0
        and float(gray.std()) >= 8.0
        and colour_count >= 24
    )


class PhoneCamera:
    """Latest-frame camera with an iVCam-compatible interface."""

    PREFERRED_HINTS = ("ivcam", "e2esoft", "phone", "droidcam")

    def __init__(
        self,
        index: int = 0,
        width: int = 1280,
        height: int = 720,
        rotate_clockwise: bool = True,
        warmup_frames: int = 3,
        crop: Sequence[float] = (0.0, 0.0, 1.0, 1.0),
    ):
        self.index = int(index)
        self.width = int(width)
        self.height = int(height)
        self.rotate_clockwise = bool(rotate_clockwise)
        self.warmup_frames = int(warmup_frames)
        self.crop = tuple(float(value) for value in crop)
        self._capture: cv2.VideoCapture | None = None
        self._frame_condition = threading.Condition()
        self._reader_stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._latest_frame: np.ndarray | None = None
        self._latest_started = 0.0
        self._latest_received = 0.0
        self._frame_sequence = 0
        self.last_frame_metadata: dict[str, float | int] = {}

    def _candidates(self) -> list[tuple[int, int]]:
        names = _device_names()
        preferred = [
            position
            for position, name in enumerate(names)
            if any(hint in name.casefold() for hint in self.PREFERRED_HINTS)
        ]
        candidates: list[tuple[int, int]] = []
        if preferred:
            candidates.extend((cv2.CAP_DSHOW, position) for position in preferred)
            candidates.extend((cv2.CAP_MSMF, position) for position in preferred)
        else:
            for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW):
                for position in (self.index, 0, 1, 2, 3):
                    if position >= 0 and (backend, position) not in candidates:
                        candidates.append((backend, position))
        return candidates

    def open(self) -> "PhoneCamera":
        if self._capture and self._capture.isOpened():
            return self
        last_error = "no candidate camera responded"
        for backend, position in self._candidates():
            capture = cv2.VideoCapture(position, backend)
            if not capture.isOpened():
                capture.release()
                continue
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            usable = False
            for _ in range(max(3, self.warmup_frames)):
                ok, sample = capture.read()
                if ok and sample is not None and _looks_alive(sample):
                    usable = True
            if usable:
                self._capture = capture
                self._reader_stop.clear()
                self._reader_thread = threading.Thread(
                    target=self._read_latest, daemon=True, name="phone-latest-frame"
                )
                self._reader_thread.start()
                return self
            capture.release()
            last_error = f"candidate {backend}/{position} returned dark or static frames"
        raise VisionError(f"Cannot open a working camera for index {self.index}: {last_error}")

    def _read_latest(self) -> None:
        capture = self._capture
        while capture is not None and not self._reader_stop.is_set():
            started = time.monotonic()
            ok, frame = capture.read()
            received = time.monotonic()
            if not ok or frame is None:
                self._reader_stop.wait(0.03)
                continue
            with self._frame_condition:
                self._latest_frame = frame
                self._latest_started = started
                self._latest_received = received
                self._frame_sequence += 1
                self._frame_condition.notify_all()

    def close(self) -> None:
        self._reader_stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
            self._reader_thread = None
        if self._capture:
            self._capture.release()
            self._capture = None
        with self._frame_condition:
            self._latest_frame = None

    def __enter__(self) -> "PhoneCamera":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def frame(self) -> np.ndarray:
        self.open()
        requested = time.monotonic()
        deadline = requested + 5.0
        with self._frame_condition:
            while self._latest_frame is None or self._latest_started < requested:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VisionError("Timed out waiting for a new camera frame")
                self._frame_condition.wait(remaining)
            frame = self._latest_frame.copy()
            self.last_frame_metadata = {
                "sequence": self._frame_sequence,
                "read_started_monotonic": self._latest_started,
                "received_monotonic": self._latest_received,
                "frame_age_ms": round((time.monotonic() - self._latest_received) * 1000, 1),
            }
        if not _looks_alive(frame):
            raise VisionError("Camera is disconnected or returning a plain waiting screen")
        if self.rotate_clockwise:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        left, top, right, bottom = self.crop
        height, width = frame.shape[:2]
        x1 = int(np.clip(left, 0, 1) * width)
        y1 = int(np.clip(top, 0, 1) * height)
        x2 = int(np.clip(right, 0, 1) * width)
        y2 = int(np.clip(bottom, 0, 1) * height)
        if x2 <= x1 or y2 <= y1:
            raise VisionError(f"Invalid normalized crop: {self.crop}")
        return frame[y1:y2, x1:x2].copy()


# The rest of this project historically imported the class under this name.
IvcamCamera = PhoneCamera
