import tempfile
import wave
from faster_whisper import WhisperModel
from typing import Dict, Optional
import numpy as np


def pcm_to_wav_path(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Write 16-bit PCM to a temp WAV for Whisper. Returns path. For IVR/streaming."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    with wave.open(f.name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return f.name


class ASRService:
    def __init__(self):
        self.model = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8"
        )
    
    def transcribe_with_confidence(
        self, 
        audio_path: str, 
        language: Optional[str] = "en",
        use_vad_filter: bool = False
    ) -> Dict:
        """
        Transcribe audio and return structured result with confidence
        
        Args:
            audio_path: Path to audio file
            language: Language code (default: "en")
            use_vad_filter: If True, use Whisper's VAD (default: False for live turns)
                           Set to False when turn-end is already determined by real-time VAD
        
        Returns:
        {
            "text": "transcribed text",
            "confidence": 0.0-1.0,
            "language": "en"
        }
        """
        try:
            segments, info = self.model.transcribe(
                audio_path,
                language=language,
                vad_filter=use_vad_filter,  # Issue 3: Disabled for live turns
                beam_size=5
            )
            
            # Collect segments with confidence scores
            full_text = []
            confidences = []
            detected_language = info.language
            
            for segment in segments:
                full_text.append(segment.text)
                # Convert log probability to confidence (0-1 scale)
                # Whisper returns log probabilities, convert to confidence
                confidence = min(1.0, max(0.0, np.exp(segment.avg_logprob)))
                confidences.append(confidence)
            
            # Combine text
            text = " ".join(full_text).strip()
            
            # Calculate average confidence
            avg_confidence = np.mean(confidences) if confidences else 0.0
            
            # Handle empty results
            if not text:
                return {
                    "text": "",
                    "confidence": 0.0,
                    "language": detected_language or "en"
                }
            
            return {
                "text": text,
                "confidence": float(avg_confidence),
                "language": detected_language or "en"
            }
            
        except Exception as e:
            print(f"ASR Error: {e}")
            return {
                "text": "",
                "confidence": 0.0,
                "language": "en"
            }
    
    def detect_silence(self, audio_path: str, threshold: float = 0.01) -> bool:
        """
        Detect if audio contains mostly silence
        Returns True if audio is mostly silent
        """
        try:
            import librosa
            audio, sr = librosa.load(audio_path, sr=16000)
            rms = librosa.feature.rms(y=audio)[0]
            avg_rms = np.mean(rms)
            return avg_rms < threshold
        except ImportError:
            # librosa not available, use simple fallback
            # Check file size - very small files are likely silence
            try:
                import os
                file_size = os.path.getsize(audio_path)
                return file_size < 1000  # Less than 1KB likely silence
            except:
                return False
        except Exception as e:
            print(f"Silence detection error: {e}")
            # Fallback: if ASR returns empty, likely silence
            return True

