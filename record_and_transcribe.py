import os, asyncio, sounddevice as sd, numpy as np
from google import genai
from google.genai import types
RATE=16000; SEC=10
print('RECORDING_NOW_SPEAK_FOR_10_SECONDS', flush=True)
a=sd.rec(int(RATE*SEC), samplerate=RATE, channels=1, dtype='int16', device=1)
sd.wait(); data=a.tobytes(); print('RECORDING_DONE_BYTES='+str(len(data)), flush=True)
async def main():
 c=genai.Client(api_key=os.environ['GEMINI_API_KEY'])
 cfg=types.LiveConnectConfig(response_modalities=['AUDIO'], input_audio_transcription=types.AudioTranscriptionConfig())
 async with c.aio.live.connect(model='gemini-3.1-flash-live-preview', config=cfg) as s:
  print('LIVE_CONNECTED', flush=True)
  await s.send_realtime_input(audio=types.Blob(data=data,mime_type='audio/pcm;rate=16000'))
  await asyncio.sleep(2)
  try:
   async for r in s.receive():
    c=r.server_content
    if c and c.input_transcription and c.input_transcription.text: print('TRANSCRIPT='+c.input_transcription.text, flush=True)
    if c and c.turn_complete: print('TURN_COMPLETE',flush=True); break
  except Exception as e: print('RECEIVE_ERROR='+repr(e),flush=True)
asyncio.run(main())
