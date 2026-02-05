import os
import tempfile
import time
import uvicorn
import base64
import asyncio
import json
from fastapi import FastAPI, UploadFile, File, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import requests
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ---------- Speech to Text ----------
from faster_whisper import WhisperModel

# ---------- Gemini ----------
# from google import genai
from groq import Groq

# ---------- Session Management ----------
from session_manager import SessionManager, SessionState

# ---------- Conversation Engine ----------
from conversation_engine import ConversationEngine, ConversationState

# ---------- ASR Service ----------
from asr_service import ASRService, pcm_to_wav_path
from confidence_router import ConfidenceAction

# ---------- Turn-taking (VAD + timing + barge-in) ----------
from turn_taking import TurnTakingEngine, TurnEvent, TurnEventType, has_voice, vad_probability

load_dotenv()

# ==================================================
# CONFIG
# ==================================================
# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# if not GOOGLE_API_KEY:
#     raise RuntimeError("GOOGLE_API_KEY not set in environment (.env)")
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# client = genai.Client(api_key=GOOGLE_API_KEY)

# ==================================================
# SESSION MANAGEMENT
# ==================================================
try:
    session_manager = SessionManager(
        redis_host=os.getenv("REDIS_HOST", "localhost"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        redis_db=int(os.getenv("REDIS_DB", "0")),
        idle_timeout=int(os.getenv("IDLE_TIMEOUT", "30")),
        max_session_duration=int(os.getenv("MAX_SESSION_DURATION", "600"))
    )
    print("✅ Session Manager initialized successfully")
except Exception as e:
    print(f"⚠️  Warning: Session Manager initialization failed: {e}")
    print("⚠️  Running without session management. Install and start Redis for full functionality.")
    session_manager = None

# ==================================================
# CONVERSATION ENGINE
# ==================================================
conversation_engine = None
if session_manager:
    try:
        conversation_engine = ConversationEngine(
            redis_client=session_manager.redis_client,
            groq_client=groq_client
        )
        print("✅ Conversation Engine initialized successfully")
    except Exception as e:
        print(f"⚠️  Warning: Conversation Engine initialization failed: {e}")
        conversation_engine = None

# Background task for idle session cleanup
async def cleanup_task():
    while True:
        await asyncio.sleep(10)  # Check every 10 seconds
        if session_manager:
            try:
                session_manager.cleanup_idle_sessions()
            except Exception as e:
                print(f"Error in cleanup task: {e}")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    if session_manager:
        asyncio.create_task(cleanup_task())
    yield
    # Shutdown (if needed)

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================================================
# ASR SERVICE
# ==================================================
asr_service = ASRService()

# Legacy function for backward compatibility
def speech_to_text(audio_path: str) -> str:
    """Legacy function - use asr_service.transcribe_with_confidence for new code"""
    result = asr_service.transcribe_with_confidence(audio_path)
    return result.get("text", "")

def text_to_speech_elevenlabs(text: str) -> str:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2",
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.85
        }
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code != 200:
        raise RuntimeError(f"ElevenLabs error: {response.text}")

    audio_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    audio_file.write(response.content)
    audio_file.close()

    return audio_file.name


def _audio_duration_sec(path: str) -> float:
    """Get duration of audio file for barge-in timing. IVR-friendly."""
    try:
        import librosa
        return float(librosa.get_duration(path=path))
    except Exception:
        return 3.0


# ==================================================
# SYSTEM PROMPT
# ==================================================
SYSTEM_PROMPT = """
You are a friendly and professional dental clinic voice assistant for SmileCare Dental Clinic.

Your main goal is to help users:
- Book dental appointments
- Answer basic clinic information questions

Speak in short, clear, natural, spoken-language sentences.
Do not give medical advice or pricing details.

--------------------------------------------------
CLINIC DETAILS

Clinic Name: SmileCare Dental Clinic
Working Days: Monday to Saturday
Clinic Timings: 9:00 AM to 7:00 PM
Location: 2nd Floor, Green Plaza, MG Road

DOCTORS:
1. Dr. Ananya Sharma
   - Specialty: General Dentistry
   - Available: Monday to Friday, 9:00 AM to 1:00 PM

2. Dr. Rohan Mehta
   - Specialty: Orthodontist (Braces)
   - Available: Monday, Wednesday, Friday, 3:00 PM to 7:00 PM

SERVICES:
- Tooth pain
- Teeth cleaning
- Braces consultation
- Routine checkup

AVAILABLE DEMO SLOTS (HARD-CODED):
- Tomorrow 10:00 AM – Dr. Ananya Sharma
- Tomorrow 11:30 AM – Dr. Ananya Sharma
- Tomorrow 4:00 PM – Dr. Rohan Mehta

--------------------------------------------------
ONE-SHOT EXAMPLE CONVERSATION

User: Hi, I want to book a dental appointment.
Assistant: Sure! I can help with that. May I know your name?

User: My name is Rahul.
Assistant: Thanks, Rahul. What problem are you facing today?

User: I have tooth pain.
Assistant: I’m sorry to hear that. For tooth pain, Dr. Ananya Sharma is available. When would you like to visit?

User: Tomorrow morning.
Assistant: Tomorrow morning we have two slots available: 10:00 AM or 11:30 AM. Which one works for you?

User: 11:30 AM.
Assistant: Your appointment is confirmed.
Patient name: Rahul.
Doctor: Dr. Ananya Sharma.
Date: Tomorrow.
Time: 11:30 AM.
Clinic: SmileCare Dental Clinic.
Please arrive 10 minutes early.
Would you like me to send this confirmation by SMS?

--------------------------------------------------
BEHAVIOR RULES

- Always guide the user step by step.
- Ask only one question at a time.
- Always confirm appointment details clearly at the end.
- If a user asks something outside scope, politely redirect to booking or clinic timings.
- Keep responses short and voice-friendly.
"""

# ==================================================
# CHAT RESPONSE WITH SESSION MANAGEMENT
# ==================================================
MAX_TURNS = 12  # demo safe

def get_doctor_reply(user_text: str, session_id: Optional[str] = None) -> str:
    # Build messages for LLM
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Get history from Redis if session exists
    if session_manager and session_id:
        history = session_manager.get_history(session_id, limit=MAX_TURNS * 2)
        # Filter out timestamp field - Groq only accepts role and content
        for msg in history:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
    
    # Add current user message
    messages.append({"role": "user", "content": user_text})
    
    # Get reply from LLM
    completion = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.4,
        max_tokens=150,
    )

    reply = completion.choices[0].message.content.strip()
    return reply



# ==================================================
# API ENDPOINTS
# ==================================================
@app.post("/voice")
async def voice_chat(
    audio: UploadFile = File(...),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    # Session management
    session_id = None
    session_state = None
    
    if session_manager:
        # Create or retrieve session
        if not x_session_id:
            session_id = session_manager.create_session()
            # Initialize conversation engine for new session
            if conversation_engine:
                conversation_engine.initialize(session_id)
        else:
            session = session_manager.get_session(x_session_id)
            if not session:
                # Session expired or invalid, create new one
                session_id = session_manager.create_session()
                if conversation_engine:
                    conversation_engine.initialize(session_id)
            else:
                session_id = x_session_id
                # Check if session should be closed
                if session_manager.check_timeout(session_id):
                    session_manager.close_session(session_id)
                    session_id = session_manager.create_session()
                    if conversation_engine:
                        conversation_engine.initialize(session_id)
                # Update activity
                session_manager.update_activity(session_id)
        
        session_state = session_manager.get_session_state(session_id)
    
    # Process audio
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
        f.write(await audio.read())
        audio_path = f.name

    # Use enhanced ASR with confidence
    asr_result = asr_service.transcribe_with_confidence(audio_path)
    user_text = asr_result.get("text", "")
    confidence = asr_result.get("confidence", 0.0)
    language = asr_result.get("language", "en")
    
    print(f"[Session {session_id}] User: {user_text} (confidence: {confidence:.2f}, lang: {language})")
    
    # Conversation Engine State Management with confidence routing
    should_end = False
    conversation_response = ""
    conv_state = None
    metadata = {}
    
    if conversation_engine and session_id:
        # Use confidence-based processing if we have text
        if user_text and confidence > 0:
            conv_state, conversation_response, should_end, metadata = (
                conversation_engine.process_asr_result(session_id, asr_result)
            )
        else:
            # Empty or no confidence - use regular state transition
            conv_state, conversation_response, should_end = conversation_engine.process_state_transition(
                session_id, user_text
            )
            metadata = {"confidence": confidence, "action": "EMPTY"}
        
        print(f"[Session {session_id}] Conversation State: {conv_state.value}, Action: {metadata.get('action', 'N/A')}")
        
        # If conversation ended, close session
        if should_end:
            if session_manager:
                session_manager.close_session(session_id)
    
    # Determine final response
    if should_end:
        reply = conversation_response or "Thank you for calling. Have a great day!"
    elif conversation_response:
        # Conversation engine provided a response (greeting, clarification, silence prompt, etc.)
        reply = conversation_response
    elif not user_text:
        # No input detected - conversation engine should have handled this, but fallback
        reply = conversation_response or "I'm listening. Please go ahead."
    else:
        # Normal conversation flow - use LLM when in RESPONDING state
        if conversation_engine and session_id and conv_state == ConversationState.RESPONDING:
            # Generate LLM response
            reply = get_doctor_reply(user_text, session_id)
            
            # Add to history
            if session_manager and session_id:
                session_manager.add_to_history(session_id, "user", user_text)
                session_manager.add_to_history(session_id, "assistant", reply)
            
            # Transition back to listening after response
            conversation_engine.process_state_transition(session_id, None)
        else:
            # Fallback: conversation engine not available or unexpected state
            reply = get_doctor_reply(user_text, session_id)
            if session_manager and session_id:
                session_manager.add_to_history(session_id, "user", user_text)
                session_manager.add_to_history(session_id, "assistant", reply)
    
    print(f"[Session {session_id}] Assistant:", reply)

    # Generate audio using ElevenLabs
    audio_file_path = text_to_speech_elevenlabs(reply)
    
    # Read audio file and encode as base64
    with open(audio_file_path, "rb") as audio_file:
        audio_data = audio_file.read()
        audio_base64 = base64.b64encode(audio_data).decode("utf-8")
    
    # Clean up temporary file
    try:
        os.unlink(audio_file_path)
        os.unlink(audio_path)
    except:
        pass
    
    response_data = {
        "text": reply,
        "audio": audio_base64
    }
    
    if session_id:
        response_data["session_id"] = session_id
        response_data["session_state"] = session_state.value if session_state else "UNKNOWN"
        
        if conversation_engine:
            conv_state = conversation_engine.get_state(session_id)
            response_data["conversation_state"] = conv_state.value if conv_state else "UNKNOWN"
            response_data["should_end"] = should_end
        
        # Add ASR metadata
        response_data["asr_confidence"] = confidence
        response_data["asr_language"] = language
        response_data["asr_action"] = metadata.get("action", "N/A")
    
    return JSONResponse(response_data)

@app.get("/session/{session_id}/status")
async def get_session_status(session_id: str):
    """Get current session status"""
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session management not available")
    
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return {
        "session_id": session_id,
        "state": session["state"],
        "created_at": session["created_at"],
        "last_activity": session["last_activity"],
        "is_idle": session_manager.check_idle(session_id)
    }

@app.post("/session/create")
async def create_session_endpoint():
    """Create a new session and return session ID"""
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session management not available")
    
    session_id = session_manager.create_session()
    
    # Initialize conversation engine for new session
    if conversation_engine:
        conversation_engine.initialize(session_id)
    
    return {
        "session_id": session_id,
        "message": "Session created"
    }

@app.post("/session/{session_id}/close")
async def close_session_endpoint(session_id: str):
    """Manually close a session"""
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session management not available")
    
    session_manager.close_session(session_id)
    return {"message": "Session closed", "session_id": session_id}

# ==================================================
# WEBSOCKET STREAMING ENDPOINT
# ==================================================
@app.websocket("/ws/voice/{session_id}")
async def websocket_voice(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for streaming ASR
    Enables two-way continuous communication
    """
    await websocket.accept()
    
    try:
        # Verify session exists
        if session_manager:
            session = session_manager.get_session(session_id)
            if not session:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid session"
                })
                await websocket.close()
                return
            
            # Initialize conversation if needed
            if conversation_engine:
                conv_state = conversation_engine.get_state(session_id)
                if conv_state is None:
                    conversation_engine.initialize(session_id)

        # Human-like turn-taking: VAD + timing + barge-in (PCM path). WebM uses legacy buffer.
        # Issue 5: Per-session configurable thresholds (can be customized per user/language)
        turn_config = {}  # Can be loaded from session or user profile in future
        turn_engine = TurnTakingEngine(config=turn_config)
        bot_speaking = False
        bot_speaking_until = 0.0
        audio_buffer = []  # for legacy WebM path only
        pcm_buffer = bytearray()  # Accumulate small PCM chunks
        chunk_count = 0  # Debug counter
        last_log_time = time.time()  # Throttle debug logs
        
        # Debug: Log VAD availability
        from turn_taking import _VAD_AVAILABLE
        print(f"[Session {session_id}] WebSocket connected. VAD available: {_VAD_AVAILABLE}")
        
        # Send greeting if first connection
        if conversation_engine:
            conv_state = conversation_engine.get_state(session_id)
            if conv_state == ConversationState.INIT:
                greeting_state, greeting_text, _ = conversation_engine.process_state_transition(
                    session_id, None
                )
                # Generate TTS for greeting
                audio_path = text_to_speech_elevenlabs(greeting_text)
                with open(audio_path, "rb") as f:
                    audio_data = f.read()
                    audio_base64 = base64.b64encode(audio_data).decode("utf-8")

                await websocket.send_json({
                    "type": "response",
                    "text": greeting_text,
                    "audio": audio_base64,
                    "conversation_state": greeting_state.value
                })
                bot_speaking_until = time.time() + _audio_duration_sec(audio_path)
                bot_speaking = True
                os.unlink(audio_path)
        
        # ----- Main loop: PCM (turn-taking + barge-in) or legacy WebM -----
        while True:
            try:
                data = await websocket.receive()
            except WebSocketDisconnect:
                break

            if "text" in data:
                try:
                    msg = json.loads(data["text"])
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "end":
                        break
                except json.JSONDecodeError:
                    pass
                continue

            if "bytes" not in data:
                continue

            chunk = data["bytes"]
            chunk_count += 1
            
            # Debug: Log chunk info periodically (every 2 seconds)
            current_time = time.time()
            if current_time - last_log_time >= 2.0:
                print(f"[Session {session_id}] Chunk #{chunk_count}: size={len(chunk)} bytes, "
                      f"pcm_buffer={len(pcm_buffer)} bytes, state={turn_engine.state.value}")
                last_log_time = current_time
            
            # Legacy WebM: e.g. browser MediaRecorder. No VAD/turn-taking.
            is_webm = len(chunk) >= 4 and chunk[:4] == b'\x1aE\xdf\xa3'
            if is_webm:
                audio_buffer.append(chunk)
                if len(audio_buffer) >= 50:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
                        for c in audio_buffer:
                            f.write(c)
                        p = f.name
                    asr_result = await asyncio.to_thread(asr_service.transcribe_with_confidence, p)
                    is_silence = asr_service.detect_silence(p)
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
                    audio_buffer = []
                    if not is_silence and asr_result.get("text"):
                        if conversation_engine:
                            conv_state, response, should_end, metadata = conversation_engine.process_asr_result(session_id, asr_result)
                            await websocket.send_json({"type": "transcription", "text": asr_result["text"], "confidence": asr_result["confidence"], "language": asr_result.get("language", "en"), "action": metadata.get("action", "ACCEPT")})
                            if response:
                                ap = text_to_speech_elevenlabs(response)
                                with open(ap, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                await websocket.send_json({"type": "response", "text": response, "audio": b64, "conversation_state": conv_state.value, "should_end": should_end})
                                os.unlink(ap)
                                if should_end:
                                    break
                            elif conv_state == ConversationState.RESPONDING:
                                llm = await asyncio.to_thread(get_doctor_reply, asr_result["text"], session_id)
                                if session_manager:
                                    session_manager.add_to_history(session_id, "user", asr_result["text"])
                                    session_manager.add_to_history(session_id, "assistant", llm)
                                ap = text_to_speech_elevenlabs(llm)
                                with open(ap, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                await websocket.send_json({"type": "response", "text": llm, "audio": b64, "conversation_state": conv_state.value})
                                conversation_engine.process_state_transition(session_id, None)
                                os.unlink(ap)
                continue

            # ----- PCM path: Accumulate chunks until we have enough data -----
            FRAME_SIZE = 640  # 20ms at 16kHz, 16-bit PCM
            
            # Accumulate all incoming chunks
            pcm_buffer.extend(chunk)
            
            # Debug: Log when we accumulate enough
            if len(pcm_buffer) >= FRAME_SIZE and len(pcm_buffer) < FRAME_SIZE * 2:
                print(f"[Session {session_id}] PCM buffer ready: {len(pcm_buffer)} bytes")
            
            # Process when we have at least one complete frame
            while len(pcm_buffer) >= FRAME_SIZE:
                # Extract one frame (640 bytes)
                frame = bytes(pcm_buffer[:FRAME_SIZE])
                pcm_buffer = pcm_buffer[FRAME_SIZE:]
                
                # Debug: Log VAD detection
                vad_prob = vad_probability(frame)
                if vad_prob > 0.0:  # Only log when there's some signal
                    print(f"[Session {session_id}] Frame processed: size={len(frame)}, "
                          f"VAD_prob={vad_prob:.3f}, state={turn_engine.state.value}")
                
                try:
                    if bot_speaking:
                        # Issue 6: Require 2 consecutive speech frames for barge-in
                        if vad_prob >= 0.6:
                            # Track consecutive speech frames
                            if not hasattr(turn_engine, '_barge_in_frames'):
                                turn_engine._barge_in_frames = 0
                            turn_engine._barge_in_frames += 1
                            
                            # Require 2 consecutive frames (40-60ms of speech) for reliable barge-in
                            if turn_engine._barge_in_frames >= 2:
                                bot_speaking = False
                                turn_engine._barge_in_frames = 0
                                await websocket.send_json({"type": "barge_in"})
                        else:
                            # Reset counter on silence or low probability
                            if hasattr(turn_engine, '_barge_in_frames'):
                                turn_engine._barge_in_frames = 0
                            
                            if time.time() <= bot_speaking_until:
                                continue
                            bot_speaking = False
                except Exception as e:
                    print(f"[Session {session_id}] Error in barge-in detection: {e}")
                    pass

                # Process frame through turn-taking engine
                old_state = turn_engine.state.value  # Track state change
                ev = turn_engine.process_chunk(frame)
                
                # Debug: Log state transitions
                if turn_engine.state.value != old_state:
                    print(f"[Session {session_id}] State transition: {old_state} -> {turn_engine.state.value}, "
                          f"VAD_prob={vad_prob:.3f}")
                
                if ev is None:
                    continue
                
                # Debug: Log turn-taking events
                print(f"[Session {session_id}] Turn event: {ev.type.value}, state={turn_engine.state.value}")

                # Handle turn-taking events
                if ev.type == TurnEventType.NUDGE:
                    # Prevent infinite NUDGE loop - check silence prompt count
                    silence_prompts = 0
                    if conversation_engine:
                        conv_data = conversation_engine.get_conversation_data(session_id)
                        silence_prompts = int(conv_data.get("silence_prompts", "0") or "0")
                    
                    # If we've already sent 3+ nudges, stop asking and wait for user
                    if silence_prompts >= 3:
                        print(f"[Session {session_id}] Too many nudges ({silence_prompts}), waiting silently...")
                        continue
                    
                    print(f"[Session {session_id}] NUDGE triggered (silence_prompts={silence_prompts}, state={turn_engine.state.value})")
                    
                    if conversation_engine:
                        conversation_engine.increment_silence_prompt(session_id)
                    msg = conversation_engine.get_nudge_message() if conversation_engine else "Are you still there?"
                    ap = text_to_speech_elevenlabs(msg)
                    with open(ap, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    bot_speaking_until = time.time() + _audio_duration_sec(ap)
                    bot_speaking = True
                    await websocket.send_json({"type": "response", "text": msg, "audio": b64, "conversation_state": "LISTENING"})
                    os.unlink(ap)
                    continue

                if ev.type == TurnEventType.COMFORT:
                    msg = conversation_engine.get_comfort_message() if conversation_engine else "Take your time, I'm listening."
                    ap = text_to_speech_elevenlabs(msg)
                    with open(ap, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    bot_speaking_until = time.time() + _audio_duration_sec(ap)
                    bot_speaking = True
                    await websocket.send_json({"type": "response", "text": msg, "audio": b64, "conversation_state": "LISTENING"})
                    os.unlink(ap)
                    continue

                if ev.type == TurnEventType.CONTINUATION_CUE:
                    msg = conversation_engine.get_continuation_cue() if conversation_engine else "Mm-hmm… go on."
                    ap = text_to_speech_elevenlabs(msg)
                    with open(ap, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    bot_speaking_until = time.time() + _audio_duration_sec(ap)
                    bot_speaking = True
                    await websocket.send_json({"type": "response", "text": msg, "audio": b64, "conversation_state": "LISTENING"})
                    os.unlink(ap)
                    turn_engine.finalize_turn()
                    continue

                if ev.type == TurnEventType.TURN_END and ev.buffer:
                    buffer_size = len(ev.buffer)
                    buffer_duration_ms = (buffer_size / 2) / 16  # 16-bit samples at 16kHz
                    print(f"[Session {session_id}] TURN_END: buffer_size={buffer_size} bytes ({buffer_duration_ms:.1f}ms)")
                    
                    # Only process if buffer has minimum audio (at least 100ms = 3200 bytes for very short utterances)
                    if buffer_size < 3200:
                        print(f"[Session {session_id}] Buffer too short ({buffer_size} bytes, {buffer_duration_ms:.1f}ms), skipping ASR")
                        turn_engine.finalize_turn()
                        continue
                    
                    # Check buffer energy to see if it's actually audio or silence
                    import struct
                    energy_sum = 0.0
                    sample_count = 0
                    for i in range(0, min(len(ev.buffer) - 1, 1000), 2):  # Sample first 1000 bytes
                        try:
                            sample, = struct.unpack_from("<h", ev.buffer, i)
                            energy_sum += abs(sample)
                            sample_count += 1
                        except (struct.error, IndexError):
                            break
                    avg_energy = (energy_sum / sample_count / 32768.0) if sample_count > 0 else 0.0
                    print(f"[Session {session_id}] Buffer energy check: avg={avg_energy:.4f} (should be >0.01 for speech)")
                    
                    wav_path = pcm_to_wav_path(ev.buffer)
                    print(f"[Session {session_id}] Created WAV file: {wav_path}, size={os.path.getsize(wav_path)} bytes")
                    
                    # Issue 3: Disable Whisper VAD for live turns (we already determined turn-end)
                    try:
                        asr_result = await asyncio.to_thread(
                            asr_service.transcribe_with_confidence, 
                            wav_path,
                            use_vad_filter=False
                        )
                        print(f"[Session {session_id}] ASR raw result: {asr_result}")
                    except Exception as e:
                        print(f"[Session {session_id}] ASR ERROR: {e}")
                        asr_result = {"text": "", "confidence": 0.0, "language": "en"}
                    
                    try:
                        os.unlink(wav_path)
                    except Exception:
                        pass
                    text = (asr_result.get("text") or "").strip()
                    confidence = asr_result.get("confidence", 0.0)
                    print(f"[Session {session_id}] ASR: '{text}' conf={confidence:.2f}, buffer_duration={buffer_duration_ms:.1f}ms, energy={avg_energy:.4f}")

                    incomplete = False
                    if conversation_engine and text:
                        complete, _ = conversation_engine.check_linguistic_completeness(text)
                        if not complete:
                            turn_engine.turn_end_incomplete()
                            incomplete = True

                    if not incomplete:
                        turn_engine.finalize_turn()

                    # Even if ASR fails, check if we should still process (very low threshold for debugging)
                    if not text or confidence < 0.1:  # Very low threshold to catch any speech
                        print(f"[Session {session_id}] ASR failed: text='{text}', conf={confidence:.2f}, energy={avg_energy:.4f} - skipping conversation engine")
                        # If energy is high but ASR failed, there might be an issue with the audio format
                        if avg_energy > 0.01:
                            print(f"[Session {session_id}] WARNING: High energy ({avg_energy:.4f}) but ASR returned empty - possible audio format issue")
                        continue

                    if incomplete:
                        print(f"[Session {session_id}] Incomplete utterance detected, waiting for continuation")
                        continue

                    print(f"[Session {session_id}] Calling conversation_engine.process_asr_result...")
                    if conversation_engine:
                        conv_state, response, should_end, metadata = conversation_engine.process_asr_result(session_id, asr_result)
                        print(f"[Session {session_id}] Conversation engine response: state={conv_state.value if conv_state else 'None'}, "
                              f"response='{response[:50] if response else 'None'}...', should_end={should_end}, action={metadata.get('action', 'N/A')}")
                    else:
                        conv_state, response, should_end, metadata = None, "", False, {}
                        print(f"[Session {session_id}] WARNING: conversation_engine is None!")

                    await websocket.send_json({
                        "type": "transcription",
                        "text": text,
                        "confidence": confidence,
                        "language": asr_result.get("language", "en"),
                        "action": metadata.get("action", "ACCEPT"),
                    })

                    if response:
                        print(f"[Session {session_id}] Sending response from conversation_engine: '{response[:100]}...'")
                        ap = text_to_speech_elevenlabs(response)
                        with open(ap, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("utf-8")
                        bot_speaking_until = time.time() + _audio_duration_sec(ap)
                        bot_speaking = True
                        await websocket.send_json({"type": "response", "text": response, "audio": b64, "conversation_state": (conv_state.value if conv_state else "LISTENING"), "should_end": should_end})
                        print(f"[Session {session_id}] Response sent successfully")
                        os.unlink(ap)
                        if should_end:
                            break
                    elif conv_state == ConversationState.RESPONDING:
                        print(f"[Session {session_id}] State is RESPONDING, calling LLM for: '{text}'")
                        llm = await asyncio.to_thread(get_doctor_reply, text, session_id)
                        print(f"[Session {session_id}] LLM response: '{llm[:100]}...'")
                        if session_manager:
                            session_manager.add_to_history(session_id, "user", text)
                            session_manager.add_to_history(session_id, "assistant", llm)
                        ap = text_to_speech_elevenlabs(llm)
                        with open(ap, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("utf-8")
                        bot_speaking_until = time.time() + _audio_duration_sec(ap)
                        bot_speaking = True
                        await websocket.send_json({"type": "response", "text": llm, "audio": b64, "conversation_state": conv_state.value})
                        print(f"[Session {session_id}] LLM response sent successfully")
                        os.unlink(ap)
                        conversation_engine.process_state_transition(session_id, None)
                    else:
                        print(f"[Session {session_id}] WARNING: No response and state is not RESPONDING. State={conv_state.value if conv_state else 'None'}, response='{response}'")
    
    except WebSocketDisconnect:
        print(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass

# ==================================================
# RUN SERVER
# ==================================================
if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="localhost",
        port=8000,
        reload=True
    )
