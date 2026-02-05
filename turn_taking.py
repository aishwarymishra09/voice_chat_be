"""
Human-like turn-end detection for IVR/voice: VAD + timing + linguistic cues.
All processing is server-side. Production-grade, non-robotic behavior.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict

try:
    import webrtcvad
    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # 640 at 16kHz

# Human-like timing (ms)
SILENCE_GRACE_MS = 1000      # Initial silence detection (increased from 700ms)
CONFIRMATION_WINDOW_MS = 400 # Additional wait to confirm end (NEW - prevents premature cutoffs)
MIN_SPEECH_MS = 300          # Min speech before processing (avoid noise)
LONG_SILENCE_NUDGE_MS = 1500 # No speech at all -> "Are you still there?"
INCOMPLETE_WAIT_MS = 300     # If incomplete, wait before "Mm-hmm… go on."
COMFORT_WAIT_MS = 1500       # In incomplete wait -> "Take your time, I'm listening"

CHUNK_MS = 200


def vad_probability(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """
    Returns VAD probability (0.0-1.0) for the chunk.
    Processes in 20-30ms frames for better accuracy.
    Returns: 0.0 (no speech), 1.0 (clear speech), 0.5 (uncertain)
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0
    
    # If chunk is smaller than a frame, use energy-based detection
    if len(pcm_bytes) < FRAME_BYTES:
        import struct
        total = 0.0
        count = 0
        for i in range(0, len(pcm_bytes) - 1, 2):
            try:
                s, = struct.unpack_from("<h", pcm_bytes, i)
                total += abs(s)
                count += 1
            except (struct.error, IndexError):
                break
        if count == 0:
            return 0.0
        avg = total / count / 32768.0
        # Convert energy to probability (0.0-1.0) - adjusted thresholds
        if avg > 0.03:  # Lowered from 0.05 for better sensitivity
            return 1.0
        elif avg > 0.015:  # Lowered from 0.02
            return 0.5
        elif avg > 0.005:  # Lowered from 0.01
            return 0.3
        return 0.0
    
    if not _VAD_AVAILABLE:
        # Fallback: energy-based probability with better thresholds
        import struct
        total = 0.0
        count = 0
        for i in range(0, len(pcm_bytes) - 1, 2):
            try:
                s, = struct.unpack_from("<h", pcm_bytes, i)
                total += abs(s)
                count += 1
            except (struct.error, IndexError):
                break
        if count == 0:
            return 0.0
        avg = total / count / 32768.0
        # Convert energy to probability (0.0-1.0) - adjusted thresholds
        if avg > 0.03:
            return 1.0
        elif avg > 0.015:
            return 0.5
        elif avg > 0.005:
            return 0.3
        return 0.0
    
    # Process in 20ms frames (more accurate than processing whole chunk)
    try:
        vad = webrtcvad.Vad(2)  # Aggressiveness: 0-3, 2 is balanced
        speech_frames = 0
        total_frames = 0
        
        for i in range(0, len(pcm_bytes) - FRAME_BYTES + 1, FRAME_BYTES):
            frame = pcm_bytes[i : i + FRAME_BYTES]
            if len(frame) == FRAME_BYTES:  # Ensure full frame
                try:
                    if vad.is_speech(frame, sample_rate):
                        speech_frames += 1
                    total_frames += 1
                except Exception:
                    # Skip invalid frames
                    continue
        
        if total_frames == 0:
            # Fallback to energy-based if no valid frames
            import struct
            total = 0.0
            count = 0
            for i in range(0, min(len(pcm_bytes) - 1, 1000), 2):  # Sample first 1000 bytes
                try:
                    s, = struct.unpack_from("<h", pcm_bytes, i)
                    total += abs(s)
                    count += 1
                except (struct.error, IndexError):
                    break
            if count > 0:
                avg = total / count / 32768.0
                if avg > 0.03:
                    return 1.0
                elif avg > 0.015:
                    return 0.5
                elif avg > 0.005:
                    return 0.3
            return 0.0
        
        # Probability = ratio of speech frames
        prob = speech_frames / total_frames
        
        # Threshold mapping for more nuanced detection
        if prob >= 0.5:  # Lowered from 0.6 for better sensitivity
            return 1.0  # Clear speech
        elif prob >= 0.25:  # Lowered from 0.3
            return 0.5  # Uncertain/mixed
        elif prob > 0.0:
            return 0.3  # Weak signal
        return 0.0  # No speech
    except Exception as e:
        # If webrtcvad fails, fallback to energy-based
        import struct
        total = 0.0
        count = 0
        for i in range(0, min(len(pcm_bytes) - 1, 1000), 2):
            try:
                s, = struct.unpack_from("<h", pcm_bytes, i)
                total += abs(s)
                count += 1
            except (struct.error, IndexError):
                break
        if count == 0:
            return 0.0
        avg = total / count / 32768.0
        if avg > 0.03:
            return 1.0
        elif avg > 0.015:
            return 0.5
        elif avg > 0.005:
            return 0.3
        return 0.0


def has_voice(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> bool:
    """
    Returns True if speech detected (probability > 0.6).
    Used for backward compatibility.
    """
    prob = vad_probability(pcm_bytes, sample_rate)
    return prob > 0.6


def has_voice_uncertain(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> Optional[bool]:
    """
    Returns: True (speech), False (silence), None (uncertain).
    Used for more nuanced turn-taking decisions.
    """
    prob = vad_probability(pcm_bytes, sample_rate)
    # Lowered threshold for better sensitivity in IDLE state
    if prob >= 0.1:  # Lowered threshold for better sensitivity
        return True
    elif prob < 0.05:  # Very low = definitely silence
        return False
    return None  # Uncertain - don't change state


class TurnEventType(Enum):
    TURN_END = "TURN_END"
    CONTINUATION_CUE = "CONTINUATION_CUE"
    NUDGE = "NUDGE"
    COMFORT = "COMFORT"


@dataclass
class TurnEvent:
    type: "TurnEventType"
    buffer: Optional[bytes] = None


class TurnState(Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    CANDIDATE_END = "CANDIDATE_END"  # Initial silence detected, waiting to confirm
    WAITING_INCOMPLETE = "WAITING_INCOMPLETE"


class TurnTakingEngine:
    """
    Multi-signal turn-end: VAD + grace window + confirmation + min speech.
    Two-stage detection prevents cutting users off during natural pauses.
    Emits: TURN_END, CONTINUATION_CUE, NUDGE, COMFORT.
    Supports per-session configurable thresholds.
    """

    def __init__(self, chunk_ms: int = CHUNK_MS, config: Optional[Dict] = None):
        self.chunk_ms = chunk_ms
        self.state = TurnState.IDLE
        self.buffer = bytearray()
        self.silence_chunks = 0
        self.speech_chunks = 0
        self.idle_silence_chunks = 0
        self.consecutive_speech_frames = 0  # For barge-in (Issue 6)
        
        # Per-session configurable thresholds (Issue 5)
        config = config or {}
        silence_grace = config.get("candidate_end_ms", SILENCE_GRACE_MS)
        confirmation = config.get("final_end_ms", CONFIRMATION_WINDOW_MS)
        min_speech = config.get("min_speech_ms", MIN_SPEECH_MS)
        nudge = config.get("nudge_ms", LONG_SILENCE_NUDGE_MS)
        incomplete = config.get("incomplete_wait_ms", INCOMPLETE_WAIT_MS)
        comfort = config.get("comfort_wait_ms", COMFORT_WAIT_MS)
        
        self._silence_grace_chunks = max(1, int(round(silence_grace / chunk_ms)))
        self._confirmation_chunks = max(1, int(round(confirmation / chunk_ms)))
        self._min_speech_chunks = max(1, int(round(min_speech / chunk_ms)))
        self._nudge_chunks = max(1, int(round(nudge / chunk_ms)))
        self._incomplete_wait_chunks = max(1, int(round(incomplete / chunk_ms)))
        self._comfort_wait_chunks = max(1, int(round(comfort / chunk_ms)))

    def process_chunk(self, pcm_bytes: bytes) -> Optional[TurnEvent]:
        if not pcm_bytes:
            return None
        
        # Validate chunk size - must be at least one frame (640 bytes for 20ms at 16kHz)
        if len(pcm_bytes) < FRAME_BYTES:
            # Too small, skip but accumulate if we're already listening
            if self.state == TurnState.LISTENING or self.state == TurnState.CANDIDATE_END:
                self.buffer.extend(pcm_bytes)
            return None

        # Use probabilistic VAD (Issue 2)
        vad_result = has_voice_uncertain(pcm_bytes)
        vad_prob = vad_probability(pcm_bytes)
        
        # Handle uncertain cases - don't change state
        if vad_result is None:
            # Uncertain - accumulate but don't change state
            if self.state == TurnState.LISTENING or self.state == TurnState.CANDIDATE_END:
                self.buffer.extend(pcm_bytes)
            # In IDLE, uncertain might still be speech - be more lenient
            elif self.state == TurnState.IDLE and vad_prob >= 0.1:
                # Treat uncertain as potential speech in IDLE to avoid getting stuck
                self.state = TurnState.LISTENING
                self.buffer = bytearray(pcm_bytes)
                self.speech_chunks = 1
                self.silence_chunks = 0
                self.idle_silence_chunks = 0
            return None
        
        voice = vad_result

        if self.state == TurnState.IDLE:
            if voice:
                print(f"[TurnEngine] IDLE -> LISTENING (VAD detected speech, prob={vad_prob:.3f})")
                self.state = TurnState.LISTENING
                self.buffer = bytearray(pcm_bytes)
                self.speech_chunks = 1
                self.silence_chunks = 0
                self.idle_silence_chunks = 0
            else:
                self.idle_silence_chunks += 1
                if self.idle_silence_chunks >= self._nudge_chunks:
                    print(f"[TurnEngine] IDLE: NUDGE triggered after {self.idle_silence_chunks} silence chunks (VAD_prob={vad_prob:.3f})")
                    self.idle_silence_chunks = 0
                    return TurnEvent(TurnEventType.NUDGE, None)
            return None

        if self.state == TurnState.LISTENING:
            if voice:
                self.buffer.extend(pcm_bytes)
                self.speech_chunks += 1
                self.silence_chunks = 0  # Reset silence counter on speech
            else:
                self.buffer.extend(pcm_bytes)
                self.silence_chunks += 1
                # After initial silence grace period, enter candidate end state
                if self.silence_chunks >= self._silence_grace_chunks:
                    if self.speech_chunks >= self._min_speech_chunks:
                        # Enter candidate end - wait for confirmation window
                        self.state = TurnState.CANDIDATE_END
                        self.silence_chunks = 0  # Reset for confirmation window
                    else:
                        # Not enough speech, likely noise - reset
                        self.state = TurnState.IDLE
                        self.buffer.clear()
                        self.speech_chunks = 0
                        self.silence_chunks = 0
            return None

        if self.state == TurnState.CANDIDATE_END:
            if voice:
                # User resumed speaking - continue listening (speech continuation detected)
                self.state = TurnState.LISTENING
                self.buffer.extend(pcm_bytes)
                self.speech_chunks += 1
                self.silence_chunks = 0
            else:
                # Still silent - accumulate and check confirmation window
                self.buffer.extend(pcm_bytes)
                self.silence_chunks += 1
                # Only emit TURN_END after confirmation window passes
                if self.silence_chunks >= self._confirmation_chunks:
                    return TurnEvent(TurnEventType.TURN_END, bytes(self.buffer))
            return None

        if self.state == TurnState.WAITING_INCOMPLETE:
            if voice:
                self.state = TurnState.LISTENING
                self.buffer.extend(pcm_bytes)
                self.speech_chunks += 1
                self.silence_chunks = 0
            else:
                self.silence_chunks += 1
                if self.silence_chunks >= self._comfort_wait_chunks:
                    self._reset()
                    return TurnEvent(TurnEventType.COMFORT, None)
                if self.silence_chunks >= self._incomplete_wait_chunks:
                    self._reset()
                    return TurnEvent(TurnEventType.CONTINUATION_CUE, None)
            return None

        return None

    def turn_end_incomplete(self):
        """Call when ASR + linguistic check say incomplete. Keeps buffer, waits 300ms."""
        self.state = TurnState.WAITING_INCOMPLETE
        self.silence_chunks = 0

    def finalize_turn(self):
        """Call when turn is fully processed."""
        self._reset()

    def _get_vad_probability(self, pcm_bytes: bytes) -> float:
        """Helper for barge-in detection."""
        return vad_probability(pcm_bytes)
    
    def _reset(self):
        self.state = TurnState.IDLE
        self.buffer.clear()
        self.speech_chunks = 0
        self.silence_chunks = 0
        self.idle_silence_chunks = 0
        self.consecutive_speech_frames = 0

