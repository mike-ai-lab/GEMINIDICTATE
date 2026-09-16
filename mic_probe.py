import sounddevice as sd
import numpy as np
import time
print('MIC PROBE: speak continuously for 8 seconds...', flush=True)
levels=[]
def cb(indata, frames, time_info, status):
    if status: print('STATUS:', status, flush=True)
    rms=float(np.sqrt(np.mean(indata[:,0].astype(np.float64)**2)))
    peak=int(np.max(np.abs(indata[:,0])))
    levels.append(rms)
    print(f'RMS={rms:.1f} PEAK={peak}', flush=True)
with sd.InputStream(device=1, samplerate=44100, channels=1, dtype='int16', blocksize=4410, callback=cb):
    time.sleep(8)
print('PROBE_DONE max_rms=', max(levels) if levels else 0, flush=True)
