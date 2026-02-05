# Human-Like Turn-Taking Implementation

## Overview

This implementation provides **production-grade, human-like turn-taking** for IVR/voice calls using a **hybrid strategy**: VAD + conversational timing + linguistic cues + interruption handling.

## Architecture

### Core Components

1. **`turn_taking.py`** - Turn-end detection engine

   - VAD (Voice Activity Detection) using `webrtcvad`
   - Timing logic (700ms silence grace, 300ms min speech, etc.)
   - State machine: IDLE → LISTENING → SILENCE_AFTER_SPEECH → WAITING_INCOMPLETE
   - Emits events: TURN_END, CONTINUATION_CUE, NUDGE, COMFORT

2. **`asr_service.py`** - ASR with PCM support

   - `pcm_to_wav_path()` - Converts 16-bit PCM to WAV for Whisper
   - Maintains existing `transcribe_with_confidence()` for WebM

3. **`conversation_engine.py`** - Linguistic intelligence

   - `check_linguistic_completeness()` - Uses LLM to detect incomplete thoughts
   - `get_nudge_message()` - "Are you still there?"
   - `get_comfort_message()` - "Take your time, I'm listening."
   - `get_continuation_cue()` - "Mm-hmm… go on."

4. **`app.py`** - WebSocket endpoint

   - **PCM path**: Full turn-taking with VAD, barge-in, linguistic checks
   - **WebM path**: Legacy support (backward compatible)
   - Barge-in detection: Stops TTS when user speaks
   - All processing is **server-side**

5. **`index.html`** - Frontend PCM capture
   - Uses `ScriptProcessorNode` to capture raw PCM
   - Downsamples from 48kHz/44.1kHz to 16kHz if needed
   - Sends 16-bit PCM chunks (~200ms) via WebSocket
   - Handles `barge_in` messages to stop audio playback

## How It Works

### Turn-End Detection Flow

```
User speaks → VAD detects voice
  ↓
Accumulate audio in buffer
  ↓
User stops speaking → 700ms silence detected
  ↓
Check: Minimum 300ms of speech? (avoid noise)
  ↓ YES
Emit TURN_END with accumulated buffer
  ↓
Server: Convert PCM → WAV → ASR
  ↓
Linguistic completeness check
  ↓
  ├─ COMPLETE → Process with conversation engine → LLM → TTS
  └─ INCOMPLETE → Wait 300ms more
      ├─ User resumes → Continue listening
      └─ Still silent → Emit CONTINUATION_CUE ("Mm-hmm… go on.")
          └─ After 1.5s total → Emit COMFORT ("Take your time...")
```

### Barge-In Flow

```
Bot speaking (TTS playing)
  ↓
User starts speaking
  ↓
VAD detects voice in incoming chunk
  ↓
Send `barge_in` message to client
  ↓
Client stops audio playback immediately
  ↓
Continue processing user's speech
```

### Edge Cases Handled

1. **Long silence (1.5s) with no speech**: Emit NUDGE → "Are you still there?"
2. **User pauses a lot during incomplete wait**: Emit COMFORT → "Take your time, I'm listening."
3. **Incomplete thought detected**: Wait 300ms, then CONTINUATION_CUE
4. **Background noise**: Minimum 300ms speech requirement filters out noise
5. **User interrupts bot**: Barge-in stops TTS immediately

## Timing Constants

- **SILENCE_GRACE_MS = 700ms**: Wait this long after speech before considering turn end
- **MIN_SPEECH_MS = 300ms**: Minimum speech duration to avoid noise triggers
- **LONG_SILENCE_NUDGE_MS = 1500ms**: No speech at all → nudge
- **INCOMPLETE_WAIT_MS = 300ms**: Wait for continuation after incomplete detection
- **COMFORT_WAIT_MS = 1500ms**: Extended wait → comfort message

## Audio Format

- **Input**: 16-bit PCM, 16kHz, mono (from frontend or IVR gateway)
- **Processing**: Converted to WAV for Whisper ASR
- **Output**: MP3 from ElevenLabs TTS

## IVR Integration

For IVR calls, the telephony gateway should:

1. Send **16-bit PCM, 16kHz, mono** to WebSocket `/ws/voice/{session_id}`
2. Receive JSON messages: `transcription`, `response`, `barge_in`, `error`
3. Handle `barge_in` by stopping any TTS playback

## Testing

1. Start Redis: `redis-server`
2. Install dependencies: `pip install -r requirements.txt`
3. Set environment variables in `.env`:
   - `GROQ_API_KEY`
   - `ELEVENLABS_API_KEY`
   - `ELEVENLABS_VOICE_ID`
   - `REDIS_HOST`, `REDIS_PORT` (optional)
4. Run server: `python app.py`
5. Open `http://localhost:8000` in browser
6. Click "Start Recording" and speak naturally

## Key Features

✅ **Human-like timing** - 700ms grace window prevents cutting users off  
✅ **Linguistic intelligence** - Detects incomplete thoughts using LLM  
✅ **Barge-in support** - Users can interrupt bot naturally  
✅ **Noise filtering** - 300ms minimum speech requirement  
✅ **Gentle prompts** - NUDGE, COMFORT, CONTINUATION_CUE for edge cases  
✅ **All server-side** - Frontend only captures and sends audio  
✅ **Production-ready** - Handles errors, timeouts, disconnections gracefully

## Files Modified

- ✅ `turn_taking.py` (NEW)
- ✅ `asr_service.py` (added `pcm_to_wav_path`)
- ✅ `conversation_engine.py` (added linguistic methods)
- ✅ `app.py` (complete WebSocket rewrite)
- ✅ `index.html` (PCM capture + barge_in handler)
- ✅ `requirements.txt` (added `webrtcvad`)

## Next Steps

The system is ready for IVR integration. All processing happens server-side, making it perfect for telephony gateways that send PCM audio streams.
