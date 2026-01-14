# import os
# import re
# import json
# import tempfile
# import uvicorn
# from fastapi import FastAPI, UploadFile, File
#
# # ---------- Speech to Text (Windows-safe) ----------
# from faster_whisper import WhisperModel
#
# # ---------- Google AI Studio (NEW SDK) ----------
# from google import genai
# from fastapi.middleware.cors import CORSMiddleware
#
# # ==================================================
# # CONFIG
# # ==================================================
# GOOGLE_API_KEY = "AIzaSyA7_j27zwKcmSFSv1N-IYWuKEam9PHmFYg"
#eleven labs = sk_635b130275feaedfd059a9446de4f1384ac62a741c39ef32
# voice id = "mfMM3ijQgz8QtMeKifko"
# if not GOOGLE_API_KEY:
#     raise RuntimeError("GOOGLE_AI_STUDIO_KEY not set")
#
# client = genai.Client(api_key=GOOGLE_API_KEY)
#
# app = FastAPI()
#
# # ==================================================
# # LOAD WHISPER MODEL (FREE, LOCAL)
# # ==================================================
# whisper_model = WhisperModel(
#     "base",
#     device="cpu",
#     compute_type="int8"
# )
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[
#         "http://localhost:63342",  # PyCharm HTML preview
#         "http://127.0.0.1:63342",
#         "http://localhost",
#         "http://127.0.0.1"
#     ],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
#
#
# def speech_to_text(audio_path: str) -> str:
#     segments, _ = whisper_model.transcribe(
#         audio_path,
#         language="en",          # ✅ FORCE ENGLISH
#         task="transcribe",      # default, explicit
#         vad_filter=True         # optional, improves quality
#     )
#     return " ".join(seg.text for seg in segments).strip()
#
# # ==================================================
# # GEMINI FIELD EXTRACTION (FREE TIER)
# # ==================================================
# def extract_json_from_text(text: str) -> dict | None:
#     """
#     Extracts the first valid JSON object from a string.
#     Handles ```json fences, markdown, and extra text.
#     """
#
#     if not text:
#         return None
#
#     # 1️⃣ Remove markdown code fences if present
#     text = text.strip()
#
#     # Remove ```json or ``` wrappers
#     text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
#     text = re.sub(r"```$", "", text).strip()
#
#     # 2️⃣ Extract JSON object { ... }
#     match = re.search(r"\{.*\}", text, re.DOTALL)
#     if not match:
#         return None
#
#     json_text = match.group(0)
#
#     # 3️⃣ Parse JSON
#     try:
#         return json.loads(json_text)
#     except json.JSONDecodeError:
#         return None
# def extract_fields(user_text: str) -> dict:
#     prompt = f"""
# You are a healthcare appointment intake assistant.
# for any greeting messages return
# Return ONLY valid JSON:
# {{
#   "name": null,
#   "phone": null,
#   "symptoms": null,
#   "needs_rephrase": false
# }}
#
# Rules:
# - Do NOT invent information
# - Phone must contain digits, optional +
# - If text is unclear or empty, set needs_rephrase = true
#
# User text:
# {user_text}
# """
#     try:
#         response = client.models.generate_content(
#             model="models/gemini-flash-latest",
#             contents=[
#                 {
#                     "role": "user",
#                     "parts": [{"text": prompt}]
#                 }
#             ]
#         )
#         json_op= extract_json_from_text(response.candidates[0].content.parts[0].text)
#         return json_op
#
#     except Exception as e:
#         print("Gemini error:", e)
#         return {"needs_rephrase": True}
#
# # ==================================================
# # SIMPLE CONVERSATION STATE (DEMO)
# # ==================================================
# STATE = {
#     "name": None,
#     "phone": None,
#     "symptoms": None
# }
#
# def valid_phone(phone: str) -> bool:
#     return re.fullmatch(r"\+?\d{8,15}", phone or "") is not None
#
# def next_question() -> str:
#     if not STATE["name"]:
#         return "What is your full name?"
#     if not STATE["phone"]:
#         return "What phone number can our doctor contact you on?"
#     if not STATE["symptoms"]:
#         return "Please describe your symptoms."
#     return "Thank you. Our doctor will contact you soon."
#
# # ==================================================
# # API ENDPOINT
# # ==================================================
# @app.post("/voice")
# async def voice_chat(audio: UploadFile = File(...)):
#     with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
#         f.write(await audio.read())
#         audio_path = f.name
#
#     user_text = speech_to_text(audio_path)
#     print(user_text)
#     if not user_text:
#         return {"text": "I didn’t catch that. Please rephrase."}
#
#     data = extract_fields(user_text)
#
#     if data.get("needs_rephrase"):
#         return {"text": f"I didn’t understand. {next_question()}"}
#
#     if data.get("name") and not STATE["name"]:
#         STATE["name"] = data["name"]
#
#     if data.get("phone") and not STATE["phone"] and valid_phone(data["phone"]):
#         STATE["phone"] = data["phone"]
#
#     if data.get("symptoms") and not STATE["symptoms"]:
#         STATE["symptoms"] = data["symptoms"]
#
#     return {"text": next_question()}
#
# # ==================================================
# # RUN SERVER (PYCHARM FRIENDLY)
# # ==================================================
# if __name__ == "__main__":
#     uvicorn.run(
#         "main:app",
#         host="127.0.0.1",
#         port=8000,
#         reload=True
#     )
