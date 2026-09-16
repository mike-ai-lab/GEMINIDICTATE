import asyncio, os, queue, sys, time
import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from google import genai
from google.genai import types

RATE_IN = 44100
RATE_OUT = 16000
DEVICE = 1
SECONDS = 15
q = queue.Queue()

def callback(indata, frames, time_info, status):
    if status:
        print(f'INPUT_STATUS={status}', flush=True)
    q.put(indata[:, 0].copy())

async def main():
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name='Puck'))),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text=(
            'You are a transcription engine. Do not answer or converse. '
            'Transcribe the user audio accurately. Preserve the spoken language. '
            'Use normal punctuation and capitalization. Do not invent words.'
        ))])
    )
    print('CONNECTING...', flush=True)
    async with client.aio.live.connect(model='gemini-3.1-flash-live-preview', config=cfg) as session:
        print('CONNECTED', flush=True)
        print('SPEAK NOW — 15 SECONDS', flush=True)
        start = time.monotonic()
        async def sender():
            with sd.InputStream(samplerate=RATE_IN, channels=1, dtype='int16', blocksize=882, device=DEVICE, callback=callback):
                while time.monotonic() - start < SECONDS:
                    try:
                        x = await asyncio.to_thread(q.get, True, 0.25)
                    except queue.Empty:
                        continue
                    y = resample_poly(x, RATE_OUT, RATE_IN).astype(np.int16)
                    await session.send_realtime_input(audio=types.Blob(data=y.tobytes(), mime_type='audio/pcm;rate=16000'))
        async def receiver():
            deadline = time.monotonic() + SECONDS + 8
            while time.monotonic() < deadline:
                try:
                    async for response in session.receive():
                        sc = response.server_content
                        if sc and sc.input_transcription and sc.input_transcription.text:
                            print('TRANSCRIPT_DELTA=' + sc.input_transcription.text, flush=True)
                        if sc and sc.turn_complete:
                            print('TURN_COMPLETE', flush=True)
                            if time.monotonic() > start + SECONDS:
                                return
                except Exception as e:
                    print('RECEIVE_ERROR=' + repr(e), flush=True)
                    return
        await asyncio.gather(sender(), receiver())
    print('TEST_FINISHED', flush=True)

asyncio.run(main())
