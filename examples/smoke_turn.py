"""One spoken turn against a running pipeline, no microphone or browser needed.

Synthesizes a question with pocket-tts, streams it to the pipeline's WebSocket
the way the cockpit does (16 kHz mono int16 PCM in binary frames, then about
a second of silence so the server's VAD ends the turn), and waits for the
three things a working install produces: the transcript, a non-empty answer
from the brain, and reply audio.

Run it with the framework venv's python, which already has pocket_tts and
websockets:

    ~/speech-to-speech-main/.venv/bin/python3 examples/smoke_turn.py
    ~/speech-to-speech-main/.venv/bin/python3 examples/smoke_turn.py \\
        --ws ws://127.0.0.1:8765 --text "What is the capital of France?"

Prints `heard: ...`, `answer: ...`, `reply audio: N bytes`, then `SMOKE OK`
and exits 0. Otherwise prints one sentence naming what did not arrive and
exits 1. Progress notes go to stderr.

It changes no configuration (no config_set), but the pipeline has one shared
session: any open cockpit hears this turn too, and it lands in the chat
history like any other turn.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

TIMEOUT_S = 120.0
TARGET_RATE = 16000  # the pipeline's audio format: 16 kHz mono int16 little-endian
FRAME_BYTES = 1024  # 512 samples, the server's VAD frame; any size works
TAIL_SILENCE_S = 1.0  # same tail the cockpit sends on push-to-talk release
VOICE = "alba"


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def synthesize(text: str) -> bytes:
    from pocket_tts import TTSModel
    from pocket_tts.data.audio_utils import convert_audio

    model = TTSModel.load_model()
    state = model.get_state_for_audio_prompt(VOICE)
    audio = model.generate_audio(state, text)
    audio = convert_audio(audio.reshape(1, -1), model.sample_rate, TARGET_RATE, 1)
    pcm = (audio.reshape(-1).clamp(-1.0, 1.0) * 32767).round().short()
    return pcm.numpy().astype("<i2").tobytes()


async def run(ws_url: str, text: str) -> int:
    import websockets
    from websockets.exceptions import ConnectionClosed

    deadline = time.monotonic() + TIMEOUT_S
    try:
        ws = await asyncio.wait_for(websockets.connect(ws_url, max_size=16 * 1024 * 1024), timeout=10)
    except Exception as e:
        print(f"Could not connect to the pipeline at {ws_url} ({type(e).__name__}: {e}); is it running?")
        return 1

    heard: str | None = None
    answer = ""
    failure = ""
    audio_bytes = 0
    sent_turn = False
    closed = ""  # why the connection dropped, if it did

    async def listen() -> None:
        nonlocal heard, answer, failure, audio_bytes, closed
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    if sent_turn:
                        audio_bytes += len(msg)
                    continue
                try:
                    ev = json.loads(msg)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "transcription_completed" and sent_turn and heard is None:
                    heard = ev.get("transcript") or ""
                elif kind == "assistant_text" and sent_turn and ev.get("text") and not answer:
                    answer = ev["text"]
                elif kind == "response_failed" and sent_turn:
                    failure = ev.get("message") or "no reason given"
            closed = "the server closed it"
        except (ConnectionClosed, OSError) as e:
            closed = f"{type(e).__name__}: {e}"

    try:
        async with ws:
            listener = asyncio.create_task(listen())
            await ws.send(json.dumps({"type": "config_get"}))

            note(f"connected to {ws_url}; synthesizing the question with pocket-tts ({VOICE})...")
            try:
                pcm = await asyncio.to_thread(synthesize, text)
            except Exception as e:
                listener.cancel()
                print(f"Could not synthesize the question with pocket-tts ({type(e).__name__}: {e}).")
                return 1

            note(f"streaming {len(pcm)} bytes of speech plus {TAIL_SILENCE_S:.0f}s of silence...")
            sent_turn = True
            pcm += bytes(int(TARGET_RATE * TAIL_SILENCE_S) * 2)
            frame_s = FRAME_BYTES / 2 / TARGET_RATE
            for i in range(0, len(pcm), FRAME_BYTES):
                await ws.send(pcm[i:i + FRAME_BYTES])
                await asyncio.sleep(frame_s)  # real-time pace, like a microphone

            note("waiting for transcript, answer and reply audio...")
            while time.monotonic() < deadline:
                if listener.done() or failure or heard == "":
                    break
                if heard and answer and audio_bytes > 0:
                    # A little longer so the byte count covers more than the first chunk.
                    await asyncio.sleep(2.0)
                    break
                await asyncio.sleep(0.25)
            listener.cancel()
    except (ConnectionClosed, OSError) as e:
        closed = f"{type(e).__name__}: {e}"

    complete = heard and answer and audio_bytes > 0
    if closed and not complete and not failure and heard != "":
        print(f"The pipeline closed the connection before the turn finished ({closed}); check the pipeline log.")
        return 1
    if heard is None:
        print(f"No transcript arrived within {TIMEOUT_S:.0f}s: the pipeline did not hear the question.")
        return 1
    if heard == "":
        print("The pipeline heard an empty transcript: speech recognition returned nothing for the question.")
        return 1
    if failure:
        print(f"The brain did not answer (response_failed: {failure}); check the pipeline log and the LLM endpoint.")
        return 1
    if not answer:
        print(f"No answer from the brain within {TIMEOUT_S:.0f}s; check the pipeline log and the LLM endpoint.")
        return 1
    if audio_bytes == 0:
        print(f"The brain answered but no reply audio arrived within {TIMEOUT_S:.0f}s; check the pipeline log for a TTS error.")
        return 1

    print(f"heard: {heard}")
    print(f"answer: {answer}")
    print(f"reply audio: {audio_bytes} bytes")
    print("SMOKE OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Speak one question to a running pipeline and check it answers.")
    ap.add_argument("--ws", default="ws://127.0.0.1:8765", help="pipeline WebSocket URL (default: %(default)s)")
    ap.add_argument("--text", default="What is the capital of France?", help="question to speak (default: %(default)r)")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(asyncio.wait_for(run(args.ws, args.text), timeout=TIMEOUT_S + 15))
    except asyncio.TimeoutError:
        print(f"Gave up after {TIMEOUT_S:.0f}s without a complete turn.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
