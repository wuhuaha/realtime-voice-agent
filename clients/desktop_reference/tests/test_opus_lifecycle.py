from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from rva_desktop.audio.opus import PyAvOpusCodec


class _FakeCFunction:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        return self.result


class _FakeOpus:
    def __init__(self) -> None:
        self.opus_decoder_create = _FakeCFunction(123)
        self.opus_decode = _FakeCFunction()
        self.opus_decoder_destroy = _FakeCFunction()


class _FailingEncoder:
    def __init__(self, *, fail_open: bool) -> None:
        self.fail_open = fail_open

    def open(self) -> None:
        if self.fail_open:
            raise RuntimeError("encoder open failed")


@pytest.mark.parametrize("failure_stage", ["create", "open"])
def test_encoder_initialization_failure_releases_native_decoder(failure_stage: str) -> None:
    fake_opus = _FakeOpus()
    fake_av = ModuleType("av")
    fake_av.__file__ = __file__
    fake_av.AudioFrame = object

    class FakeCodecContext:
        @staticmethod
        def create(_codec: str, _mode: str) -> _FailingEncoder:
            if failure_stage == "create":
                raise RuntimeError("encoder create failed")
            return _FailingEncoder(fail_open=True)

    fake_av.CodecContext = FakeCodecContext

    with (
        patch.dict(sys.modules, {"av": fake_av}),
        patch("rva_desktop.audio.opus._load_opus_library", return_value=fake_opus),
        pytest.raises(RuntimeError, match=f"encoder {failure_stage} failed"),
    ):
        PyAvOpusCodec()

    assert fake_opus.opus_decoder_destroy.calls == [(123,)]
