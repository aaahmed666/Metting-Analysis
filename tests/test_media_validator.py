"""Unit tests for the media validation step (pure, dependency-free logic)."""
import pytest

from pipeline.processors.media_validator import (
    ValidationError,
    get_extension,
    validate_extension,
    validate_size,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("clip.MP4", "mp4"),
        ("a.b.WAV", "wav"),
        ("noext", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_get_extension(filename, expected):
    assert get_extension(filename) == expected


def test_validate_extension_accepts_allowed():
    assert validate_extension("meeting.mp4") == "mp4"


def test_validate_extension_rejects_unknown():
    with pytest.raises(ValidationError):
        validate_extension("malware.exe")


def test_validate_size_rejects_empty():
    with pytest.raises(ValidationError):
        validate_size(0)


def test_validate_size_rejects_oversized():
    with pytest.raises(ValidationError):
        validate_size(10 * 1024 * 1024 * 1024)  # 10 GB


def test_validate_size_accepts_normal():
    validate_size(1024)  # should not raise
