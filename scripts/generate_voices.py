"""Generate Pomodoro voice clips from voices/voices.json with VOICEVOX.

This script is local-only. The production bot ships pre-rendered WAV files
and never talks to VOICEVOX itself. Run it whenever voices/voices.json changes
or when you want to re-render with a different speaker:

    docker compose -f docker-compose.gen.yml up --build \
        --abort-on-container-exit --exit-code-from gen

Each JSON key becomes ``voices/<key>.wav`` and each value is sent to
VOICEVOX as-is. The default speaker is VOICEVOX 小夜/SAYO (ノーマル),
style id 46.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_ENGINE = os.getenv("VOICEVOX_URL", "http://localhost:50021")
DEFAULT_SPEAKER = 46  # VOICEVOX: 小夜/SAYO (ノーマル)
DEFAULT_VOICES_FILE = Path(__file__).resolve().parent.parent / "voices" / "voices.json"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "voices"

# Discord wants 48kHz stereo PCM. Match it so playback does not need a
# second resampling step before discord.py/ffmpeg sends the clip.
DISCORD_SAMPLE_RATE = 48000
DISCORD_STEREO = True

logger = logging.getLogger("generate_voices")


def load_voice_jobs(path: Path) -> list[tuple[str, str]]:
    """Load ``(stem, text)`` pairs from a voices.json file."""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ValueError(f"voices file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError("voices file must be a JSON object of stem -> text")

    jobs: list[tuple[str, str]] = []
    for stem, text in raw.items():
        if not isinstance(stem, str) or not stem:
            raise ValueError("voice clip names must be non-empty strings")
        if stem.endswith(".wav"):
            raise ValueError(f"voice clip name must not include .wav: {stem}")
        if "/" in stem or "\\" in stem:
            raise ValueError(
                f"voice clip name must not contain path separators: {stem}"
            )
        if not isinstance(text, str) or not text:
            raise ValueError(f"voice text for {stem!r} must be a non-empty string")
        jobs.append((stem, text))
    return jobs


async def wait_for_engine(
    session: aiohttp.ClientSession,
    engine_url: str,
    max_wait_seconds: float,
) -> str:
    """Poll ``/version`` until the engine answers; return its version string."""
    deadline = time.monotonic() + max_wait_seconds
    last_err: Exception | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            async with session.get(
                f"{engine_url}/version",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                resp.raise_for_status()
                return (await resp.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as e:
            last_err = e
            if attempt == 1 or attempt % 5 == 0:
                logger.info(
                    "waiting for voicevox engine at %s (attempt=%d, last=%s)",
                    engine_url,
                    attempt,
                    e.__class__.__name__,
                )
            await asyncio.sleep(1.5)
    raise RuntimeError(
        f"voicevox engine unreachable at {engine_url} after "
        f"{max_wait_seconds:.0f}s: {last_err}"
    )


async def synthesize(
    session: aiohttp.ClientSession,
    engine_url: str,
    speaker: int,
    text: str,
) -> bytes:
    """One audio_query -> synthesis round-trip; returns WAV bytes."""
    async with session.post(
        f"{engine_url}/audio_query",
        params={"text": text, "speaker": speaker},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        query = await resp.json()

    query["outputSamplingRate"] = DISCORD_SAMPLE_RATE
    query["outputStereo"] = DISCORD_STEREO

    async with session.post(
        f"{engine_url}/synthesis",
        params={"speaker": speaker},
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return await resp.read()


async def _amain(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        jobs = load_voice_jobs(args.voices_file)
    except ValueError as e:
        logger.error("%s", e)
        return 2

    logger.info(
        "rendering %d clips engine=%s speaker=%d voices=%s out=%s",
        len(jobs),
        args.engine,
        args.speaker,
        args.voices_file,
        out_dir,
    )

    async with aiohttp.ClientSession() as session:
        try:
            version = await wait_for_engine(session, args.engine, args.wait_seconds)
        except RuntimeError as e:
            logger.error(
                "%s. Start it via `docker compose -f docker-compose.gen.yml up`.",
                e,
            )
            return 1
        logger.info("voicevox engine version: %s", version)

        for stem, text in jobs:
            target = out_dir / f"{stem}.wav"
            if target.exists() and not args.force:
                logger.info("skip existing %s (use --force to overwrite)", target.name)
                continue
            logger.info("synth stem=%s text=%r", stem, text)
            data = await synthesize(session, args.engine, args.speaker, text)
            tmp = target.with_suffix(".wav.tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            logger.info("wrote %s (%d bytes)", target, len(data))

    logger.info("done")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--speaker", type=int, default=DEFAULT_SPEAKER)
    parser.add_argument("--voices-file", type=Path, default=DEFAULT_VOICES_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing wavs (default: skip)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=15.0,
        help=(
            "VOICEVOX engine startup wait budget. docker-compose.gen.yml "
            "passes 90 because CPU startup can be slow. Default: %(default)s"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
