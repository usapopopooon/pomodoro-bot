from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.generate_voices import load_voice_jobs

EXPECTED_RUNTIME_CLIPS = {
    "alarm",
    "connected",
    "end",
    "end-break",
    "end-long-break",
    "five-minutes-left",
    "one-minute-left",
    "pause",
    "resume",
    "start",
    "start-break",
    "start-long-break",
    "start-task",
}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_voice_jobs_uses_json_keys_as_wav_stems(tmp_path: Path) -> None:
    voices_file = tmp_path / "voices.json"
    _write_json(
        voices_file,
        {
            "start": "かいしします",
            "five-minutes-left": "のこりごふんです",
        },
    )

    assert load_voice_jobs(voices_file) == [
        ("start", "かいしします"),
        ("five-minutes-left", "のこりごふんです"),
    ]


def test_repo_voices_json_covers_runtime_clips() -> None:
    voices_file = Path(__file__).resolve().parent.parent / "voices" / "voices.json"
    stems = {stem for stem, _ in load_voice_jobs(voices_file)}

    assert stems == EXPECTED_RUNTIME_CLIPS
    assert "five-minute-left" not in stems


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"clip.wav": "x"}, "must not include .wav"),
        ({"../clip": "x"}, "path separators"),
        ({"clip": ""}, "non-empty string"),
    ],
)
def test_load_voice_jobs_rejects_invalid_entries(
    tmp_path: Path, payload: object, message: str
) -> None:
    voices_file = tmp_path / "voices.json"
    _write_json(voices_file, payload)

    with pytest.raises(ValueError, match=message):
        load_voice_jobs(voices_file)
