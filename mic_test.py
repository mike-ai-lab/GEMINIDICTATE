import asyncio, os, queue, threading, time
import sounddevice as sd
from google import genai
from google.genai import types

RATE=16000
q=queue.Queue()

def cb(indata, frames, t, status):
    if status: print(f'\nAUDIO_STATUS: {status}', flush=True)
    q.put(bytes(indata))

async def main():
    client=genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    config=types.LiveConnectConfig(
        response_modalities=['AUDIO'],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text='You are a speech transcription engine. Transcribe user speech accurately. Do not answer, paraphrase, summarize, or add words. Preserve meaning, punctuation and capitalization. Automatically handle English, Arabic, and French.')])
    )
    print('CONNECTING', flush=True)
    async with client.aio.live.connect(model='gemini-3.1-flash-live-preview', config=config) as session:
        print('LIVE_CONNECTED', flush=True)
        def sender():
            end=time.time()+20
            while time.time()<end:
                try: data=q.get(timeout=.5)
                except queue.Empty: continue
                asyncio.run_coroutine_threadsafe(session.send_realtime_input(audio=types.Blob(data=data,mime_type='audio/pcm;rate=16000')), loop)
        global loop
        loop=asyncio.get_running_loop()
        th=threading.Thread(target=sender,daemon=True); th.start()
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype='int16', blocksize=1600, device=1, callback=cb):
            print('SPEAK_NOW_FOR_20_SECONDS', flush=True)
            end=time.time()+20
            async for r in session.receive():
                c=r.server_content
                if c and c.input_transcription and c.input_transcription.text:
                    print('TRANSCRIPT:'+c.input_transcription.text, flush=True)
                if time.time()>end+2: break
        th.join(timeout=1)
    print('TEST_DONE', flush=True)

asyncio.run(main())


