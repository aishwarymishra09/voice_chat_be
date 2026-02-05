# Voice Chat Bot - Complete System Flow Documentation

## 📋 Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Complete User Flow](#complete-user-flow)
4. [Detailed Component Breakdown](#detailed-component-breakdown)
5. [State Machines](#state-machines)
6. [Key Features](#key-features)

---

## 🎯 Overview

This is a production-grade voice chat bot system that provides human-like conversational experiences. The system uses:

- **FastAPI** for the backend API
- **WebSocket** for real-time bidirectional communication
- **Redis** for session and conversation state management
- **Faster Whisper** for Automatic Speech Recognition (ASR)
- **Groq API** for LLM interactions
- **ElevenLabs API** for Text-to-Speech (TTS)
- **WebRTC VAD** for Voice Activity Detection
- **Custom Turn-Taking Engine** for natural conversation flow

---

## 🏗️ System Architecture

```
┌─────────────┐
│   Browser   │
│  (Frontend) │
└──────┬──────┘
       │ WebSocket (PCM Audio Stream)
       │
       ▼
┌─────────────────────────────────────────────────┐
│              FastAPI Backend                     │
│  ┌──────────────────────────────────────────┐  │
│  │  Session Manager (Redis)                 │  │
│  │  - Session lifecycle (NEW→ACTIVE→IDLE→CLOSED)│
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  Turn-Taking Engine                      │  │
│  │  - VAD (Voice Activity Detection)        │  │
│  │  - Multi-stage silence detection         │  │
│  │  - Barge-in handling                     │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  ASR Service (Faster Whisper)           │  │
│  │  - Speech-to-text conversion             │  │
│  │  - Confidence scoring                    │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  Confidence Router                       │  │
│  │  - Accept/Clarify/Reject decisions        │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  Conversation Engine                     │  │
│  │  - State machine (INIT→GREETING→...)    │  │
│  │  - Linguistic completeness check         │  │
│  │  - LLM integration                       │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  TTS Service (ElevenLabs)                │  │
│  │  - Text-to-speech generation             │  │
│  └──────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
       │
       ▼
┌─────────────┐
│    Redis    │
│  (State)    │
└─────────────┘
```

---

## 🔄 Complete User Flow

### Step 1: Session Creation

**Location:** `app.py` → `POST /session/create`

1. **User Action:** Frontend calls `POST /session/create`
2. **Backend Process:**
   - `SessionManager.create_session()` generates a unique `session_id` (UUID)
   - Session data stored in Redis with state `NEW`:
     ```python
     {
       "session_id": "uuid-here",
       "state": "NEW",
       "created_at": "2024-01-01T12:00:00",
       "last_activity": "2024-01-01T12:00:00",
       "user_id": "",
       "metadata": "{}"
     }
     ```
   - Session added to `sessions:active` set in Redis
   - Session expiration set (default: 600 seconds + 60 buffer)
3. **Conversation Initialization:**
   - `ConversationEngine.initialize(session_id)` called
   - Conversation state set to `INIT` in Redis:
     ```python
     {
       "state": "INIT",
       "turn_count": "0",
       "clarification_count": "0",
       "silence_prompts": "0"
     }
     ```
4. **Response:** Returns `{"session_id": "uuid", "message": "Session created"}`

---

### Step 2: WebSocket Connection

**Location:** `app.py` → `@app.websocket("/ws/voice/{session_id}")`

1. **User Action:** Frontend establishes WebSocket connection:

   ```javascript
   ws = new WebSocket(`ws://127.0.0.1:8000/ws/voice/${sessionId}`);
   ```

2. **Backend Process:**
   - WebSocket connection accepted
   - Session verification:
     - Checks if session exists in Redis
     - If invalid → sends error and closes connection
   - Conversation state check:
     - If conversation not initialized → calls `conversation_engine.initialize()`
   - **Turn-Taking Engine Initialization:**
     ```python
     turn_engine = TurnTakingEngine(config={})
     bot_speaking = False
     bot_speaking_until = 0.0
     ```
   - **Greeting (if state is INIT):**
     - `conversation_engine.process_state_transition(session_id, None)`
     - State transitions: `INIT` → `GREETING`
     - Generates greeting text: "Hello! How can I help you today?"
     - Converts to TTS via ElevenLabs
     - Sends to client as base64-encoded audio
     - State transitions: `GREETING` → `LISTENING`

---

### Step 3: Audio Capture (Frontend)

**Location:** `index.html` → `VoiceChatClient.startRecording()`

1. **User Action:** User clicks "Start" button
2. **Frontend Process:**
   - Requests microphone access: `navigator.mediaDevices.getUserMedia()`
   - Creates `AudioContext` with 16kHz sample rate
   - Sets up `ScriptProcessorNode` for audio processing
   - **Audio Processing:**
     - Captures Float32 audio samples
     - Converts to Int16 PCM (16-bit, mono)
     - Downsamples if needed (e.g., 48kHz → 16kHz)
     - Buffers 200ms chunks (3200 samples at 16kHz)
   - **Streaming:**
     - Sends PCM chunks via WebSocket as binary data
     - Each chunk: 200ms of audio (6400 bytes at 16kHz, 16-bit)

---

### Step 4: Audio Reception & VAD Processing

**Location:** `app.py` → WebSocket loop, `turn_taking.py` → `TurnTakingEngine.process_chunk()`

1. **Backend Receives:** Binary PCM chunk (200ms of audio)
2. **Barge-in Check (if bot is speaking):**

   ```python
   if bot_speaking:
       vad_prob = vad_probability(chunk)  # Returns 0.0-1.0
       if vad_prob >= 0.6:
           # Track consecutive speech frames
           barge_in_frames += 1
           if barge_in_frames >= 2:  # 2 frames = 40-60ms
               bot_speaking = False
               await websocket.send_json({"type": "barge_in"})
               # Frontend pauses bot audio playback
   ```

3. **Turn-Taking Engine Processing:**
   - Chunk split into 20ms frames for accurate VAD
   - `vad_probability()` called on each frame:
     - Uses `webrtcvad` (if available) or energy-based fallback
     - Returns probability: 0.0 (silence), 0.5 (uncertain), 1.0 (speech)
   - **Probabilistic Decision:**
     ```python
     vad_result = has_voice_uncertain(chunk)
     # Returns: True (speech), False (silence), None (uncertain)
     ```
   - **State Machine Logic:**
     - **IDLE:** Waiting for user to start speaking
       - If speech detected → transition to `LISTENING`
       - If silence > 1.5s → emit `NUDGE` event ("Are you still there?")
     - **LISTENING:** User is speaking
       - If speech → accumulate audio, reset silence counter
       - If silence ≥ 1000ms → transition to `CANDIDATE_END`
     - **CANDIDATE_END:** Initial silence detected, waiting to confirm
       - If speech resumes → back to `LISTENING` (user continued)
       - If silence continues ≥ 400ms → emit `TURN_END` event
     - **WAITING_INCOMPLETE:** Waiting for user to continue incomplete thought
       - If speech → back to `LISTENING`
       - If silence ≥ 300ms → emit `CONTINUATION_CUE` ("Mm-hmm… go on.")
       - If silence ≥ 1500ms → emit `COMFORT` ("Take your time, I'm listening.")

---

### Step 5: Turn-End Detection & ASR Processing

**Location:** `app.py` → WebSocket loop (TURN_END handler), `asr_service.py`

1. **Turn-End Event Received:**

   ```python
   if ev.type == TurnEventType.TURN_END and ev.buffer:
       # ev.buffer contains accumulated PCM audio
   ```

2. **PCM to WAV Conversion:**

   - `pcm_to_wav_path(ev.buffer)` creates temporary WAV file
   - Format: 16-bit PCM, mono, 16kHz

3. **ASR Processing (Non-blocking):**

   ```python
   asr_result = await asyncio.to_thread(
       asr_service.transcribe_with_confidence,
       wav_path,
       use_vad_filter=False  # We already determined turn-end
   )
   ```

   - **Faster Whisper Model:**
     - Model: "base" (CPU, int8)
     - Processes WAV file
     - Returns segments with text and log probabilities
   - **Confidence Calculation:**
     ```python
     confidence = np.exp(segment.avg_logprob)  # Convert log prob to 0-1
     avg_confidence = np.mean(confidences)
     ```
   - **Result:**
     ```python
     {
       "text": "Hi, I have a tooth pain.",
       "confidence": 0.48,
       "language": "en"
     }
     ```
   - Temporary WAV file deleted

4. **Linguistic Completeness Check:**
   ```python
   complete, cue = conversation_engine.check_linguistic_completeness(text)
   ```
   - **Level 1 (Fast, Rule-based):**
     - Checks for incomplete patterns:
       - Trailing: "...", "and", "so", "but"
       - Incomplete phrases: "I want to...", "So basically..."
       - Question words without "?"
     - If incomplete → returns `(False, "Mm-hmm… go on.")`
   - **Level 2 (LLM, if uncertain):**
     - Only called if rule-based is ambiguous
     - Uses Groq API to determine completeness
     - Reduces latency and cost
   - **If Incomplete:**
     - `turn_engine.turn_end_incomplete()` called
     - System waits for continuation
     - Returns to `WAITING_INCOMPLETE` state

---

### Step 6: Confidence Routing

**Location:** `confidence_router.py` → `ConfidenceRouter.route()`

1. **Confidence Check:**

   ```python
   action, processed_text = confidence_router.route(asr_result)
   ```

2. **Routing Logic:**

   - **confidence ≥ 0.8:** → `ACCEPT` (high confidence)
   - **confidence 0.2-0.8:** → `CLARIFY` (medium confidence)
   - **confidence < 0.2:** → `REJECT` (low confidence)

3. **Action Handling:**
   - **ACCEPT:** Text processed normally
   - **CLARIFY (confidence ≥ 0.3):** Text processed normally (no clarification prompt)
   - **CLARIFY (confidence 0.2-0.3):** Ask for clarification
   - **REJECT:** Increment clarification count, ask to repeat

---

### Step 7: Conversation Engine Processing

**Location:** `conversation_engine.py` → `process_asr_result()`

1. **State Machine Processing:**

   ```python
   conv_state, response, should_end, metadata = conversation_engine.process_asr_result(
       session_id, asr_result
   )
   ```

2. **Current State Check:**

   - **LISTENING:** Normal processing
   - **CLARIFYING:** Handle clarification response
   - **ERROR:** End conversation

3. **State Transition:**
   ```python
   state, response, should_end = conversation_engine.process_state_transition(
       session_id, processed_text
   )
   ```
   - **LISTENING → PROCESSING:**
     - Input quality check (EMPTY/UNCLEAR/CLEAR)
     - If CLEAR → `PROCESSING → RESPONDING`
     - If UNCLEAR → `PROCESSING → CLARIFYING`
   - **RESPONDING:**
     - Increment turn count
     - Check max turns (default: 20)
     - If exceeded → `RESPONDING → END`
     - Otherwise → `RESPONDING → LISTENING`

---

### Step 8: LLM Interaction

**Location:** `app.py` → `get_doctor_reply()`

1. **LLM Call (Non-blocking):**

   ```python
   llm_response = await asyncio.to_thread(
       get_doctor_reply, text, session_id
   )
   ```

2. **Context Building:**

   - Retrieves conversation history from Redis
   - Filters out `timestamp` field (Groq doesn't support it)
   - Builds messages array:
     ```python
     messages = [
         {"role": "system", "content": "You are a helpful medical assistant..."},
         {"role": "user", "content": "Hi, I have a tooth pain."},
         {"role": "assistant", "content": "I understand..."},
         # ... more history
     ]
     ```

3. **Groq API Call:**

   - Model: `llama-3.1-8b-instant`
   - Temperature: 0.7
   - Max tokens: 500
   - Returns: Assistant response text

4. **History Update:**
   - Adds user message to history
   - Adds assistant response to history
   - Stores in Redis: `conversation:{session_id}:history`

---

### Step 9: TTS Generation

**Location:** `app.py` → `text_to_speech_elevenlabs()`

1. **TTS Call:**

   ```python
   audio_path = text_to_speech_elevenlabs(response_text)
   ```

2. **ElevenLabs API:**

   - Converts text to speech
   - Returns audio file path (MP3)
   - Voice: Pre-configured voice ID

3. **Audio Duration Calculation:**

   ```python
   duration = _audio_duration_sec(audio_path)  # Uses librosa
   bot_speaking_until = time.time() + duration
   bot_speaking = True
   ```

4. **Base64 Encoding:**
   ```python
   with open(audio_path, "rb") as f:
       audio_data = f.read()
       audio_base64 = base64.b64encode(audio_data).decode("utf-8")
   ```

---

### Step 10: Response Delivery

**Location:** `app.py` → WebSocket send

1. **Response Sent:**

   ```python
   await websocket.send_json({
       "type": "response",
       "text": response_text,
       "audio": audio_base64,
       "conversation_state": conv_state.value
   })
   ```

2. **Frontend Handling:**

   - Receives JSON message
   - Decodes base64 audio
   - Plays audio via `Audio` element
   - Updates UI with transcript

3. **State Update:**
   - Conversation state updated in Redis
   - Session activity timestamp updated
   - Turn count incremented

---

### Step 11: Continuous Loop

The system continues in a loop:

1. **User speaks** → PCM chunks streamed
2. **VAD detects speech** → Accumulates audio
3. **Silence detected** → Turn-end detection
4. **ASR processes** → Text extracted
5. **Confidence routing** → Accept/Clarify/Reject
6. **Conversation engine** → State transitions
7. **LLM generates response** → Text created
8. **TTS converts** → Audio generated
9. **Response sent** → User hears bot
10. **Back to step 1** → Loop continues

---

## 🔧 Detailed Component Breakdown

### Session Manager (`session_manager.py`)

**Purpose:** Manages user sessions lifecycle

**States:**

- `NEW`: Session created, not yet active
- `ACTIVE`: Session in use
- `IDLE`: No activity for 30 seconds
- `CLOSED`: Session terminated

**Key Methods:**

- `create_session()`: Creates new session in Redis
- `get_session()`: Retrieves session data
- `update_activity()`: Updates last activity timestamp
- `check_timeout()`: Checks if session expired
- `close_session()`: Closes session

**Redis Keys:**

- `session:{session_id}`: Session data (hash)
- `sessions:active`: Set of active session IDs

---

### Turn-Taking Engine (`turn_taking.py`)

**Purpose:** Human-like turn-end detection using VAD + timing + linguistic cues

**States:**

- `IDLE`: Waiting for user to start
- `LISTENING`: User speaking
- `CANDIDATE_END`: Initial silence detected, confirming
- `WAITING_INCOMPLETE`: Waiting for continuation

**Key Features:**

- **Frame-level VAD:** Processes 200ms chunks into 20ms frames
- **Probabilistic VAD:** Returns 0.0-1.0 (not binary)
- **Two-stage silence detection:**
  - Stage 1: 1000ms silence → `CANDIDATE_END`
  - Stage 2: 400ms additional silence → `TURN_END`
- **Barge-in support:** 2 consecutive speech frames required
- **Configurable thresholds:** Per-session customization

**Events Emitted:**

- `TURN_END`: User finished speaking
- `CONTINUATION_CUE`: Incomplete thought detected
- `NUDGE`: Long silence (>1.5s)
- `COMFORT`: Very long silence during incomplete wait

---

### ASR Service (`asr_service.py`)

**Purpose:** Speech-to-text conversion with confidence scoring

**Model:** Faster Whisper "base" (CPU, int8)

**Key Features:**

- Converts PCM to WAV for Whisper
- Returns text, confidence (0.0-1.0), language
- `use_vad_filter=False` for live turns (we already determined turn-end)

**Confidence Calculation:**

```python
confidence = np.exp(segment.avg_logprob)  # Log prob → 0-1 scale
avg_confidence = np.mean(confidences)
```

---

### Confidence Router (`confidence_router.py`)

**Purpose:** Routes ASR results based on confidence scores

**Thresholds:**

- **≥ 0.8:** ACCEPT (high confidence)
- **0.2-0.8:** CLARIFY (medium confidence)
- **< 0.2:** REJECT (low confidence)

**Actions:**

- `ACCEPT`: Process normally
- `CLARIFY`: Ask for confirmation (or process if ≥ 0.3)
- `REJECT`: Ask to repeat

---

### Conversation Engine (`conversation_engine.py`)

**Purpose:** Manages conversation flow and state transitions

**States:**

- `INIT`: Session created, no speech yet
- `GREETING`: Bot speaks first
- `LISTENING`: Waiting for user speech
- `PROCESSING`: Analyzing user input
- `RESPONDING`: Bot generating response
- `CLARIFYING`: Asking for clarification
- `ERROR`: Error state, escalate to human
- `END`: Conversation ended

**Key Features:**

- **Linguistic Completeness:**
  - Level 1: Fast rule-based check
  - Level 2: LLM check (only if ambiguous)
- **Input Quality Detection:**
  - EMPTY: No text
  - UNCLEAR: Low confidence or unclear
  - CLEAR: Good input
- **Turn Management:**
  - Max turns: 20
  - Max clarifications: 2
  - Max silence prompts: 2

**Redis Keys:**

- `conversation:{session_id}`: Conversation state (hash)
- `conversation:{session_id}:history`: Chat history (list)

---

## 📊 State Machines

### Session State Machine

```
NEW → ACTIVE → IDLE → CLOSED
  ↓      ↓       ↓
  └──────┴───────┘
   (Activity)
```

### Conversation State Machine

```
INIT → GREETING → LISTENING → PROCESSING → RESPONDING → LISTENING
                                    ↓
                               CLARIFYING → LISTENING
                                    ↓
                                  ERROR → END
```

### Turn-Taking State Machine

```
IDLE → LISTENING → CANDIDATE_END → TURN_END
  ↑        ↑            ↑
  └────────┴────────────┘
   (Speech resumes)
```

---

## ✨ Key Features

### 1. Human-like Turn-Taking

- Multi-stage silence detection (1000ms + 400ms confirmation)
- Probabilistic VAD (handles soft speech, background noise)
- Frame-level processing (20ms frames for accuracy)
- Barge-in support (2-frame confirmation)

### 2. Intelligent Confidence Routing

- Accepts reasonable confidence (≥ 0.3) without clarification
- Only asks for clarification on very low confidence (0.2-0.3)
- Rejects only very poor quality (< 0.2)

### 3. Linguistic Completeness

- Fast rule-based check (70-80% of cases)
- LLM check only for ambiguous cases
- Prevents cutting off incomplete thoughts

### 4. Production-Grade Architecture

- Non-blocking ASR/LLM calls (`asyncio.to_thread`)
- Redis-based state persistence
- Session lifecycle management
- Error handling and fallbacks

### 5. Real-time Communication

- WebSocket for bidirectional streaming
- PCM audio format (16-bit, 16kHz, mono)
- Low-latency processing
- Barge-in interruption support

---

## 🔍 Example Flow Walkthrough

**User:** "Hi, I have a tooth pain."

1. **Session Created:** `session_id = "abc-123"`
2. **WebSocket Connected:** Greeting sent
3. **User Speaks:** PCM chunks streamed (200ms each)
4. **VAD Detects:** Speech → `LISTENING` state
5. **User Pauses:** 1000ms silence → `CANDIDATE_END`
6. **Confirmation:** 400ms more silence → `TURN_END` event
7. **ASR:** "Hi, I have a tooth pain." (conf=0.48)
8. **Linguistic Check:** Complete ✓
9. **Confidence Router:** CLARIFY (0.48 ≥ 0.3) → Process normally
10. **Conversation Engine:** `LISTENING → PROCESSING → RESPONDING`
11. **LLM:** "I understand you're experiencing tooth pain..."
12. **TTS:** Audio generated
13. **Response Sent:** User hears bot response
14. **State:** `RESPONDING → LISTENING` (ready for next turn)

---

## 🚀 Getting Started

1. **Install Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Start Redis:**

   ```bash
   redis-server
   ```

3. **Run Server:**

   ```bash
   python app.py
   ```

4. **Open Browser:**
   - Navigate to `http://localhost:8000`
   - Click "Start" to begin conversation

---

## 📝 Configuration

### Turn-Taking Thresholds (per-session)

```python
turn_config = {
    "candidate_end_ms": 1000,    # Initial silence detection
    "final_end_ms": 400,          # Confirmation window
    "min_speech_ms": 300,         # Minimum speech duration
    "nudge_ms": 1500,             # Long silence nudge
    "incomplete_wait_ms": 300,    # Incomplete wait
    "comfort_wait_ms": 1500       # Comfort message wait
}
```

### Confidence Thresholds

- **High:** ≥ 0.8 (ACCEPT)
- **Medium:** 0.2-0.8 (CLARIFY, but ≥ 0.3 processed normally)
- **Low:** < 0.2 (REJECT)

---

## 🎯 Best Practices

1. **Always check session validity** before processing
2. **Use non-blocking calls** for ASR/LLM (`asyncio.to_thread`)
3. **Clean up temporary files** after ASR processing
4. **Update session activity** on every interaction
5. **Handle WebSocket disconnections** gracefully
6. **Monitor confidence scores** for quality assurance

---

## 🔧 Troubleshooting

### Issue: Premature turn-end detection

**Solution:** Increase `candidate_end_ms` and `final_end_ms` in turn config

### Issue: Low confidence on valid speech

**Solution:** Check audio quality, consider using larger Whisper model

### Issue: Barge-in not working

**Solution:** Ensure 2 consecutive speech frames are detected (check VAD probability)

### Issue: Session expires too quickly

**Solution:** Increase `idle_timeout` in SessionManager

---

## 📚 Additional Resources

- **Redis Setup:** See `REDIS_SETUP.md`
- **Turn-Taking Details:** See `TURN_TAKING_IMPLEMENTATION.md`
- **API Documentation:** Available at `http://localhost:8000/docs`

---

**Last Updated:** 2024-01-01
**Version:** 1.0.0
