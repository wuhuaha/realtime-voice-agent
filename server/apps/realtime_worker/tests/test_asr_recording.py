from __future__ import annotations

import wave

from realtime_worker.observability.asr_recording import AsrWavRecorder


def test_asr_wav_recorder_writes_mono_pcm16_with_safe_room_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    saved = []
    recorder = AsrWavRecorder(
        tmp_path,
        sample_rate=16000,
        room="lab/voice:1",
        on_saved=saved.append,
    )

    recorder.write(b"\x01\x00\x02\x00")
    recorder.write(b"\x03\x00")
    recorder.close()

    assert recorder.path is not None
    assert recorder.path.name.startswith("asr-input-lab_voice_1-")
    assert saved == [recorder.path]
    with wave.open(str(recorder.path), "rb") as recorded:
        assert recorded.getnchannels() == 1
        assert recorded.getsampwidth() == 2
        assert recorded.getframerate() == 16000
        assert recorded.readframes(3) == b"\x01\x00\x02\x00\x03\x00"


def test_asr_wav_recorder_does_not_create_a_file_without_audio(tmp_path) -> None:  # type: ignore[no-untyped-def]
    recorder = AsrWavRecorder(tmp_path, sample_rate=16000, room="lab-voice")

    recorder.close()

    assert recorder.path is None
    assert list(tmp_path.iterdir()) == []
