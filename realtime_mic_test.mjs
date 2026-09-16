import { GoogleGenAI, Modality } from '@google/genai';
import { spawn } from 'node:child_process';
const ai=new GoogleGenAI({apiKey:process.env.GEMINI_API_KEY});
const config={responseModalities:[Modality.AUDIO],inputAudioTranscription:{},systemInstruction:{parts:[{text:'You are a transcription engine. Transcribe only the users speech accurately. Do not answer or paraphrase. Automatically handle English, Arabic and French.'}]}};
console.log('CONNECTING');
const session=await ai.live.connect({model:'gemini-3.1-flash-live-preview',config,callbacks:{onopen:()=>console.log('LIVE_CONNECTED'),onerror:e=>console.log('LIVE_ERROR '+(e?.message||e)),onclose:e=>console.log('LIVE_CLOSED '+(e?.reason||'')),onmessage:m=>{const c=m?.serverContent;if(c?.inputTranscription?.text) console.log('TRANSCRIPT='+c.inputTranscription.text);if(c?.turnComplete) console.log('TURN_COMPLETE')}}});
console.log('RECORDING_NOW_SPEAK_FOR_15_SECONDS');
const ff=spawn('ffmpeg',['-hide_banner','-loglevel','error','-f','dshow','-i','audio=Microphone (High Definition Audio Device)','-ac','1','-ar','16000','-f','s16le','-t','15','pipe:1']);
ff.stdout.on('data',d=>session.sendRealtimeInput({audio:{data:d.toString('base64'),mimeType:'audio/pcm;rate=16000'}}));
ff.stderr.on('data',d=>process.stderr.write(d));
await new Promise(r=>ff.on('close',r));
console.log('RECORDING_DONE');
await new Promise(r=>setTimeout(r,3000));
session.close();
console.log('TEST_DONE');

