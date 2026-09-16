import { GoogleGenAI, Modality } from '@google/genai';
import mic from 'mic';

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
const model = 'gemini-3.1-flash-live-preview';
const config = {
  responseModalities: [Modality.AUDIO],
  inputAudioTranscription: {},
  thinkingConfig: { thinkingLevel: 'minimal' },
  systemInstruction: { parts: [{ text: 'You are a speech transcription engine. Transcribe the user speech accurately. Do not answer, paraphrase, summarize, or add words. Preserve the spoken meaning, punctuation and capitalization. Support English, Arabic, and French automatically.' }] }
};

console.log('CONNECTING');
const session = await ai.live.connect({
  model,
  config,
  callbacks: {
    onopen: () => console.log('LIVE_CONNECTED'),
    onerror: (e) => console.log('LIVE_ERROR '+(e?.message || e)),
    onclose: (e) => console.log('LIVE_CLOSED '+(e?.reason || '')),
    onmessage: (m) => {
      const c = m?.serverContent;
      if (c?.inputTranscription?.text) process.stdout.write('\n[TRANSCRIPT] '+c.inputTranscription.text+'\n');
      if (c?.turnComplete) console.log('[TURN_COMPLETE]');
    }
  }
});

const microphone = mic({
  rate: '16000', channels: '1', debug: false,
  fileType: 'raw', encoding: 'signed-integer', endian: 'little', bitwidth: '16',
  exitOnSilence: 0
});
const input = microphone.getAudioStream();
input.on('data', (data) => {
  session.sendRealtimeInput({ media: { data: data.toString('base64'), mimeType: 'audio/pcm;rate=16000' } });
});
input.on('error', e => console.log('MIC_ERROR '+e.message));
microphone.start();
console.log('SPEAK_NOW_15_SECONDS');
setTimeout(async () => { microphone.stop(); await new Promise(r=>setTimeout(r,2000)); session.close(); console.log('TEST_DONE'); }, 15000);
