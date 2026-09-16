import asyncio, os, time, queue
import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from google import genai
from google.genai import types

MODEL_ID='gemini-3.1-flash-live-preview'
DEVICE=1; RATE_IN=44100; RATE_OUT=16000; SECONDS=12; q=queue.Queue(); levels=[]
def cb(indata, frames, time_info, status):
    if status: print(f'[MIC STATUS] {status}', flush=True)
    x=indata[:,0].copy(); levels.append(float(np.sqrt(np.mean(x.astype(np.float64)**2)))); q.put(x)

async def main():
    key=os.environ.get('GEMINI_API_KEY')
    if not key: raise RuntimeError('GEMINI_API_KEY is not set')
    client=genai.Client(api_key=key)
    config=types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text='Transcribe the incoming speech accurately. Do not answer or paraphrase.')])
    )
    print('CONNECTING...', flush=True)
    async with client.aio.live.connect(model=MODEL_ID, config=config) as session:
        print('CONNECTED - MIC OPENS NOW. SPEAK FOR 12 SECONDS.', flush=True)
        start=time.monotonic()
        async def sender():
            with sd.InputStream(device=DEVICE, samplerate=RATE_IN, channels=1, dtype='int16', blocksize=4410, callback=cb):
                print('MIC_OPEN=YES', flush=True)
                while time.monotonic()-start < SECONDS:
                    try: x=await asyncio.to_thread(q.get, True, .25)
                    except queue.Empty: continue
                    y=resample_poly(x, RATE_OUT, RATE_IN).astype(np.int16)
                    await session.send_realtime_input(audio=types.Blob(data=y.tobytes(), mime_type='audio/pcm;rate=16000'))
                await session.send_realtime_input(audio_stream_end=True)
                print('MIC_CLOSED=YES', flush=True)
        async def receiver():
            end=time.monotonic()+SECONDS+8
            while time.monotonic()<end:
                try:
                    response=await asyncio.wait_for(session.receive().__anext__(), timeout=1.0)
                except (asyncio.TimeoutError, StopAsyncIteration): continue
                sc=response.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    print('TRANSCRIPT='+sc.input_transcription.text, flush=True)
        await asyncio.gather(sender(), receiver())
    print(f'MIC_MAX_RMS={max(levels) if levels else 0:.1f}', flush=True)
    print('TEST_FINISHED', flush=True)

if __name__=='__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print('\nSTOPPED_BY_USER', flush=True)
