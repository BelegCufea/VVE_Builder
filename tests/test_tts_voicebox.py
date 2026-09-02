"""Unit tests for tts_voicebox module."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

import libs.tts_voicebox as tts_voicebox


def test_build_urls():
    """Verify URL builder helpers construct expected endpoints."""
    assert tts_voicebox._build_transcribe_url().endswith("/transcribe")
    assert tts_voicebox._build_generate_url().endswith("/generate")
    assert tts_voicebox._build_profile_url(123).endswith("/123")
    assert tts_voicebox._build_audio_url("abc").endswith("/abc")
    assert tts_voicebox._build_status_url("abc").endswith("/abc/status")
    assert tts_voicebox._build_cancel_url("abc").endswith("/abc/cancel")
    assert tts_voicebox._build_delete_url(123).endswith("/123")
    assert tts_voicebox._build_profiles_url().endswith("/profiles")
    assert tts_voicebox._build_import_url().endswith("/profiles/import")


@patch("libs.tts_voicebox.requests.post")
def test_transcribe_wav_success(mock_post, tmp_path):
    """Test successful transcription of a WAV file."""
    test_wav = tmp_path / "test.wav"
    test_wav.write_bytes(b"dummy")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": "hello world", "duration": 1.5}
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    text, success, duration = tts_voicebox.transcribe_wav(test_wav)

    assert text == "hello world"
    assert success is True
    assert duration == 1.5
    mock_post.assert_called_once()


@patch("libs.tts_voicebox.requests.post")
def test_transcribe_wav_retry_and_fail(mock_post, tmp_path):
    """Test transcription retry logic and failure handling."""
    test_wav = tmp_path / "test.wav"
    test_wav.write_bytes(b"dummy")

    mock_post.side_effect = Exception("Connection error")

    text, success, duration = tts_voicebox.transcribe_wav(
        test_wav, retry_count=1, retry_delay=0.0
    )

    assert "<ERROR:" in text
    assert success is False
    assert duration == 0.0
    assert mock_post.call_count == 2


@patch("libs.tts_voicebox.requests.post")
def test_submit_generation_success(mock_post):
    """Test submitting a TTS generation job."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "gen_12345"}
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    gen_id = tts_voicebox.submit_generation("Hello world", 42)
    assert gen_id == "gen_12345"
    mock_post.assert_called_once()


@patch("libs.tts_voicebox.requests.get")
def test_get_generation_status(mock_get):
    """Test fetching generation status."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "completed", "duration": 2.0}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    status = tts_voicebox.get_generation_status("gen_12345")
    assert status["status"] == "completed"
    assert status["duration"] == 2.0


@patch("libs.tts_voicebox.requests.post")
def test_cancel_generation(mock_post):
    """Test cancelling a generation."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    success, msg = tts_voicebox.cancel_generation("gen_12345")
    assert success is True
    assert "successful" in msg


@patch("libs.tts_voicebox.requests.get")
def test_download_generated_audio(mock_get, tmp_path):
    """Test downloading generated audio."""
    out_file = tmp_path / "output.wav"
    mock_resp = MagicMock()
    mock_resp.content = b"audio content"
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    tts_voicebox.download_generated_audio("gen_12345", str(out_file))
    assert out_file.read_bytes() == b"audio content"


@patch("libs.tts_voicebox.requests.delete")
def test_delete_voice_profile(mock_delete):
    """Test deleting a voice profile."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_delete.return_value = mock_resp

    success, msg = tts_voicebox.delete_voice_profile(123)
    assert success is True
    assert "successfully" in msg


@patch("libs.tts_voicebox.requests.get")
def test_list_profiles(mock_get):
    """Test listing voice profiles."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": 1, "name": "Profile 1"}]
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    profiles = tts_voicebox.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "Profile 1"


@patch("libs.tts_voicebox.requests.get")
def test_check_health_success(mock_get):
    """Test API health check success."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "ok"}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    success, payload = tts_voicebox.check_health("http://localhost:17493")
    assert success is True
    assert payload == {"status": "ok"}


@patch("libs.tts_voicebox.requests.get")
def test_check_health_failure(mock_get):
    """Test API health check failure."""
    mock_get.side_effect = Exception("Connection refused")

    success, payload = tts_voicebox.check_health("http://localhost:17493")
    assert success is False
    assert "Connection refused" in payload["error"]



@patch("libs.tts_voicebox.requests.get")
def test_wait_for_completion_completed(mock_get):
    """Test wait_for_completion with a completed SSE stream."""
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [
        b"data: {\"status\": \"running\"}",
        b"data: {\"status\": \"completed\", \"duration\": 2.5}",
    ]
    mock_resp.raise_for_status.return_value = None
    mock_resp.__enter__.return_value = mock_resp
    mock_get.return_value = mock_resp

    result = tts_voicebox.wait_for_completion("test-gen-id")
    assert result is not None
    assert result.get("status") == "completed"
    assert result.get("duration") == 2.5


@patch("libs.tts_voicebox.requests.get")
def test_wait_for_completion_failed(mock_get):
    """Test wait_for_completion with a failed SSE stream."""
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [
        b"data: {\"status\": \"failed\", \"error\": \"out of memory\"}",
    ]
    mock_resp.raise_for_status.return_value = None
    mock_resp.__enter__.return_value = mock_resp
    mock_get.return_value = mock_resp

    result = tts_voicebox.wait_for_completion("test-gen-id")
    assert result is not None
    assert result.get("status") == "failed"
    assert result.get("error") == "out of memory"


@patch("libs.tts_voicebox.requests.post")
def test_import_profile_success(mock_post, tmp_path):
    """Test successful import of a voice profile zip."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": 42, "name": "Imported Profile"}
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    fake_zip = tmp_path / "test.voicebox.zip"
    fake_zip.write_bytes(b"PK\x03\x04")

    result = tts_voicebox.import_profile(fake_zip)
    assert result is not None
    assert result.get("id") == 42
    assert result.get("name") == "Imported Profile"

