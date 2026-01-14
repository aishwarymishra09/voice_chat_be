import requests

API_KEY = "sk_635b130275feaedfd059a9446de4f1384ac62a741c39ef32"
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"

def test_model(model_id):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }
    payload = {
        "text": "Test voice",
        "model_id": model_id
    }

    r = requests.post(url, json=payload, headers=headers)
    print(model_id, "→", r.status_code, r.text[:200])

models = [
    "eleven_turbo_v2",
    "eleven_monolingual_v1",
    "eleven_multilingual_v1",
    "eleven_flash_v2"
]

for m in models:
    test_model(m)
