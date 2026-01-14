import os
import tempfile
import uvicorn
import base64
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import requests
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# ---------- Speech to Text ----------
from faster_whisper import WhisperModel

# ---------- Gemini ----------
from google import genai

load_dotenv()

# ==================================================
# CONFIG
# ==================================================
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

if not GOOGLE_API_KEY:
    raise RuntimeError("GOOGLE_API_KEY not set in environment (.env)")

client = genai.Client(api_key=GOOGLE_API_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================================================
# LOAD WHISPER (CPU ONLY)
# ==================================================
whisper_model = WhisperModel(
    "base",
    device="cpu",
    compute_type="int8"
)

def speech_to_text(audio_path: str) -> str:
    segments, _ = whisper_model.transcribe(
        audio_path,
        language="en",
        vad_filter=True
    )
    return " ".join(seg.text for seg in segments).strip()

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

# ==================================================
# SIMPLE CHAT MEMORY (DEMO – SINGLE USER)
# ==================================================
chat_history = []
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
# GEMINI CHAT RESPONSE
# ==================================================
def get_doctor_reply(user_text: str) -> str:
    # Include system instruction in the first message if chat is empty
    if not chat_history:
        first_message = f"{SYSTEM_PROMPT}\n\nUser: {user_text}"
        chat_history.append({
            "role": "user",
            "parts": [{"text": first_message}]
        })
    else:
        chat_history.append({
            "role": "user",
            "parts": [{"text": user_text}]
        })

    response = client.models.generate_content(
        model="models/gemini-flash-latest",
        contents=chat_history
    )

    reply = response.candidates[0].content.parts[0].text.strip()

    chat_history.append({
        "role": "model",
        "parts": [{"text": reply}]
    })

    return reply

# ==================================================
# API ENDPOINT
# ==================================================
@app.post("/voice")
async def voice_chat(audio: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
        f.write(await audio.read())
        audio_path = f.name

    user_text = speech_to_text(audio_path)
    print("User:", user_text)

    if not user_text:
        reply = "I didn't catch that. Could you please repeat?"
    else:
        reply = get_doctor_reply(user_text)
    
    print("Assistant:", reply)

    # Generate audio using ElevenLabs
    audio_file_path = text_to_speech_elevenlabs(reply)
    
    # Read audio file and encode as base64
    with open(audio_file_path, "rb") as audio_file:
        audio_data = audio_file.read()
        audio_base64 = base64.b64encode(audio_data).decode("utf-8")
    
    # Clean up temporary file
    try:
        os.unlink(audio_file_path)
    except:
        pass
    
    return JSONResponse({
        "text": reply,
        "audio": audio_base64
    })

# ==================================================
# RUN SERVER
# ==================================================
if __name__ == "__main__":
    uvicorn.run(
        "simple_chat:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )
