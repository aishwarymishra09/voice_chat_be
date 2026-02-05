import re
import redis
from datetime import datetime
from typing import Optional, Dict, Tuple
from enum import Enum
from groq import Groq
from confidence_router import ConfidenceRouter, ConfidenceAction


class ConversationState(Enum):
    INIT = "INIT"
    GREETING = "GREETING"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    RESPONDING = "RESPONDING"
    CLARIFYING = "CLARIFYING"
    ERROR = "ERROR"
    END = "END"


class InputQuality(Enum):
    EMPTY = "EMPTY"
    UNCLEAR = "UNCLEAR"
    CLEAR = "CLEAR"


class ConversationEngine:
    def __init__(self, redis_client: redis.Redis, groq_client: Groq):
        self.redis = redis_client
        self.groq = groq_client
        self.max_clarifications = 2
        self.max_silence_prompts = 2
        self.max_turns = 20
        self.confidence_router = ConfidenceRouter()
        
    def initialize(self, session_id: str):
        """Initialize conversation state for new session"""
        state_data = {
            "state": ConversationState.INIT.value,
            "turn_count": "0",
            "clarification_count": "0",
            "silence_prompts": "0",
            "last_user_input": "",
            "last_intent": "",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        self.redis.hset(f"conversation:{session_id}", mapping=state_data)
        return ConversationState.INIT
    
    def get_state(self, session_id: str) -> Optional[ConversationState]:
        """Get current conversation state"""
        state_str = self.redis.hget(f"conversation:{session_id}", "state")
        if not state_str:
            return None
        try:
            return ConversationState(state_str)
        except ValueError:
            return None
    
    def update_state(self, session_id: str, new_state: ConversationState):
        """Update conversation state"""
        self.redis.hset(f"conversation:{session_id}", "state", new_state.value)
        self.redis.hset(f"conversation:{session_id}", "updated_at", datetime.utcnow().isoformat())
    
    def get_conversation_data(self, session_id: str) -> Dict:
        """Get all conversation metadata"""
        data = self.redis.hgetall(f"conversation:{session_id}")
        return data if data else {}
    
    def increment_turn(self, session_id: str):
        """Increment turn count"""
        current = int(self.redis.hget(f"conversation:{session_id}", "turn_count") or "0")
        self.redis.hset(f"conversation:{session_id}", "turn_count", str(current + 1))
    
    def increment_clarification(self, session_id: str):
        """Increment clarification count"""
        current = int(self.redis.hget(f"conversation:{session_id}", "clarification_count") or "0")
        self.redis.hset(f"conversation:{session_id}", "clarification_count", str(current + 1))
    
    def increment_silence_prompt(self, session_id: str):
        """Increment silence prompt count"""
        current = int(self.redis.hget(f"conversation:{session_id}", "silence_prompts") or "0")
        self.redis.hset(f"conversation:{session_id}", "silence_prompts", str(current + 1))
    
    def analyze_input_quality(self, user_text: str, session_id: str) -> InputQuality:
        """Use LLM to analyze input quality"""
        if not user_text or len(user_text.strip()) == 0:
            return InputQuality.EMPTY
        
        # Simple heuristic first (fast path)
        text = user_text.strip()
        if len(text) < 3:
            return InputQuality.UNCLEAR
        
        # Use LLM for more nuanced detection
        prompt = f"""Analyze this user input and classify it as one of:
- CLEAR: Meaningful, understandable input
- UNCLEAR: Nonsensical, too short, or unintelligible

User input: "{user_text}"

Respond with ONLY one word: CLEAR or UNCLEAR"""
        
        try:
            response = self.groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=10
            )
            result = response.choices[0].message.content.strip().upper()
            
            if "CLEAR" in result:
                return InputQuality.CLEAR
            else:
                return InputQuality.UNCLEAR
        except Exception as e:
            print(f"Error analyzing input quality: {e}")
            # Graceful degradation: default to CLEAR if LLM fails
            return InputQuality.CLEAR if len(text) > 3 else InputQuality.UNCLEAR
    
    def get_greeting(self) -> str:
        """Generate greeting message"""
        return "Hello! Welcome to SmileCare Dental Clinic. How can I help you today?"
    
    def get_clarification_message(self, session_id: str) -> str:
        """Generate clarification message based on context"""
        count = int(self.redis.hget(f"conversation:{session_id}", "clarification_count") or "0")
        
        if count == 1:
            return "I didn't catch that clearly. Could you please repeat?"
        else:
            return "I'm still having trouble understanding. Could you speak more clearly?"
    
    def get_error_message(self) -> str:
        """Generate error/escalation message"""
        return "I'm having trouble understanding you. Let me connect you to a human representative who can assist you better."
    
    def get_silence_prompt(self, session_id: str) -> str:
        """Generate prompt when no speech detected"""
        count = int(self.redis.hget(f"conversation:{session_id}", "silence_prompts") or "0")
        
        if count == 0:
            return "I'm listening. Please go ahead and speak."
        elif count == 1:
            return "I'm still here. Please tell me how I can help you."
        else:
            return "I didn't hear anything. If you need assistance, please speak now or I'll end this call."

    def get_nudge_message(self) -> str:
        """Long silence, no speech yet. IVR-friendly."""
        return "Are you still there?"

    def get_comfort_message(self) -> str:
        """User pausing a lot during incomplete wait."""
        return "Take your time, I'm listening."

    def get_continuation_cue(self) -> str:
        """Incomplete thought, encourage to continue."""
        return "Mm-hmm… go on."

    def check_linguistic_completeness(self, text: str) -> Tuple[bool, Optional[str]]:
        """
        Two-level check: Fast rule-based first, then LLM if uncertain (Issue 4).
        (is_complete, continuation_cue or None).
        """
        if not text or len(text.strip()) < 3:
            return True, None
        
        text_lower = text.lower().strip()
        
        # Level 1: Fast rule-based check (Issue 4)
        incomplete_patterns = [
            # Trailing off
            text_lower.endswith(("...", "…", "and", "so", "but", "or", "then")),
            # Incomplete phrases
            any(text_lower.endswith(p) for p in [
                "i want to", "i need to", "i'd like to", "i'm trying to",
                "so basically", "and then", "but then", "or maybe",
                "i think", "i guess", "maybe", "perhaps"
            ]),
            # Question words without question mark
            text_lower.endswith(("what", "where", "when", "who", "how", "why")) and "?" not in text,
            # Very short after common starters
            len(text.split()) <= 3 and any(text_lower.startswith(p) for p in [
                "i want", "i need", "can you", "could you", "would you"
            ])
        ]
        
        if any(incomplete_patterns):
            return False, "Mm-hmm… go on."
        
        # Level 2: LLM check only if rule-based is uncertain
        # Check if sentence seems complete but might be ambiguous
        complete_indicators = [
            text.endswith((".", "!", "?")),
            len(text.split()) >= 5,  # Longer sentences more likely complete
            any(word in text_lower for word in ["appointment", "book", "schedule", "time", "date"])
        ]
        
        # If we have clear complete indicators, skip LLM
        if any(complete_indicators) and len(text.split()) >= 4:
            return True, None
        
        # Use LLM for ambiguous cases only (reduces latency and cost)
        prompt = """Does this utterance sound like a COMPLETE thought or sentence?
Consider: complete intent (e.g. "I want to book an appointment"), complete verb/object,
or trailing off ("I want to…", "So basically…", "And then…").
Reply with ONLY: COMPLETE or INCOMPLETE
If INCOMPLETE, add in parentheses one short continuation cue, e.g. (Mm-hmm… go on.)

User: "{}"
""".format(text.replace('"', "'")[:300])
        try:
            r = self.groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=60,
            )
            out = r.choices[0].message.content.strip().upper()
            if "INCOMPLETE" in out:
                cue = "Mm-hmm… go on."
                m = re.search(r"\(([^)]+)\)", out)
                if m:
                    cue = m.group(1).strip() or cue
                return False, cue
            return True, None
        except Exception as e:
            print(f"Linguistic check error: {e}")
            # Fallback: assume complete if LLM fails
            return True, None

    def process_state_transition(
        self, 
        session_id: str, 
        user_text: Optional[str] = None
    ) -> Tuple[ConversationState, str, bool]:
        """
        Process state machine transition
        Returns: (new_state, response_text, should_end)
        """
        current_state = self.get_state(session_id)
        
        if current_state is None:
            self.initialize(session_id)
            current_state = ConversationState.INIT
        
        # State machine logic
        if current_state == ConversationState.INIT:
            self.update_state(session_id, ConversationState.GREETING)
            return ConversationState.GREETING, self.get_greeting(), False
        
        elif current_state == ConversationState.GREETING:
            self.update_state(session_id, ConversationState.LISTENING)
            return ConversationState.LISTENING, "", False
        
        elif current_state == ConversationState.LISTENING:
            if not user_text or len(user_text.strip()) == 0:
                silence_count = int(self.redis.hget(f"conversation:{session_id}", "silence_prompts") or "0")
                self.increment_silence_prompt(session_id)
                
                if silence_count >= self.max_silence_prompts:
                    self.update_state(session_id, ConversationState.END)
                    return ConversationState.END, "Thank you for calling. Have a great day!", True
                else:
                    return ConversationState.LISTENING, self.get_silence_prompt(session_id), False
            else:
                self.redis.hset(f"conversation:{session_id}", "last_user_input", user_text)
                self.update_state(session_id, ConversationState.PROCESSING)
                return ConversationState.PROCESSING, "", False
        
        elif current_state == ConversationState.PROCESSING:
            quality = self.analyze_input_quality(user_text or "", session_id)
            
            if quality == InputQuality.EMPTY:
                self.increment_silence_prompt(session_id)
                silence_count = int(self.redis.hget(f"conversation:{session_id}", "silence_prompts") or "0")
                
                if silence_count >= self.max_silence_prompts:
                    self.update_state(session_id, ConversationState.END)
                    return ConversationState.END, "Thank you for calling. Have a great day!", True
                else:
                    self.update_state(session_id, ConversationState.LISTENING)
                    return ConversationState.LISTENING, self.get_silence_prompt(session_id), False
            
            elif quality == InputQuality.UNCLEAR:
                clarification_count = int(self.redis.hget(f"conversation:{session_id}", "clarification_count") or "0")
                self.increment_clarification(session_id)
                
                if clarification_count >= self.max_clarifications:
                    self.update_state(session_id, ConversationState.ERROR)
                    return ConversationState.ERROR, self.get_error_message(), True
                else:
                    self.update_state(session_id, ConversationState.CLARIFYING)
                    return ConversationState.CLARIFYING, self.get_clarification_message(session_id), False
            
            else:  # CLEAR
                self.update_state(session_id, ConversationState.RESPONDING)
                return ConversationState.RESPONDING, "", False
        
        elif current_state == ConversationState.CLARIFYING:
            if not user_text or len(user_text.strip()) == 0:
                self.increment_silence_prompt(session_id)
                silence_count = int(self.redis.hget(f"conversation:{session_id}", "silence_prompts") or "0")
                
                if silence_count >= self.max_silence_prompts:
                    self.update_state(session_id, ConversationState.END)
                    return ConversationState.END, "Thank you for calling. Have a great day!", True
                else:
                    self.update_state(session_id, ConversationState.LISTENING)
                    return ConversationState.LISTENING, self.get_silence_prompt(session_id), False
            else:
                self.redis.hset(f"conversation:{session_id}", "last_user_input", user_text)
                self.update_state(session_id, ConversationState.PROCESSING)
                return ConversationState.PROCESSING, "", False
        
        elif current_state == ConversationState.RESPONDING:
            self.increment_turn(session_id)
            turn_count = int(self.redis.hget(f"conversation:{session_id}", "turn_count") or "0")
            
            if turn_count >= self.max_turns:
                self.update_state(session_id, ConversationState.END)
                return ConversationState.END, "Thank you for the conversation. Have a great day!", True
            else:
                self.update_state(session_id, ConversationState.LISTENING)
                return ConversationState.LISTENING, "", False
        
        elif current_state == ConversationState.ERROR:
            self.update_state(session_id, ConversationState.END)
            return ConversationState.END, "", True
        
        elif current_state == ConversationState.END:
            return ConversationState.END, "", True
        
        # Default fallback
        self.update_state(session_id, ConversationState.LISTENING)
        return ConversationState.LISTENING, "", False
    
    def process_asr_result(
        self,
        session_id: str,
        asr_result: Dict
    ) -> Tuple[ConversationState, str, bool, Dict]:
        """
        Process ASR result with confidence routing
        
        Returns: (state, response, should_end, metadata)
        """
        confidence = asr_result.get("confidence", 0.0)
        text = asr_result.get("text", "")
        language = asr_result.get("language", "en")
        
        # Route based on confidence
        action, processed_text = self.confidence_router.route(asr_result)
        
        current_state = self.get_state(session_id)
        
        # Handle confidence-based routing
        if action == ConfidenceAction.REJECT:
            # Low confidence - treat as failure
            self.increment_clarification(session_id)
            clarification_count = int(
                self.redis.hget(f"conversation:{session_id}", "clarification_count") or "0"
            )
            
            if clarification_count >= self.max_clarifications:
                self.update_state(session_id, ConversationState.ERROR)
                return (
                    ConversationState.ERROR,
                    self.get_error_message(),
                    True,
                    {"confidence": confidence, "action": "REJECT", "language": language}
                )
            else:
                self.update_state(session_id, ConversationState.CLARIFYING)
                return (
                    ConversationState.CLARIFYING,
                    self.confidence_router.get_clarification_message(confidence),
                    False,
                    {"confidence": confidence, "action": "REJECT", "language": language}
                )
        
        elif action == ConfidenceAction.CLARIFY:
            # Medium confidence - process normally if confidence is reasonable (>= 0.3)
            # Only ask for clarification if confidence is very low (0.2-0.3)
            if confidence >= 0.3:
                # Accept and process normally for reasonable confidence (0.3-0.8)
                state, response, should_end = self.process_state_transition(session_id, processed_text)
                return state, response, should_end, {
                    "confidence": confidence,
                    "action": "ACCEPT",  # Treated as accept for processing
                    "language": language
                }
            else:
                # Very low confidence (0.2-0.3) - ask for clarification
                self.increment_clarification(session_id)
                clarification_count = int(
                    self.redis.hget(f"conversation:{session_id}", "clarification_count") or "0"
                )
                
                if clarification_count >= self.max_clarifications:
                    self.update_state(session_id, ConversationState.ERROR)
                    return (
                        ConversationState.ERROR,
                        self.get_error_message(),
                        True,
                        {"confidence": confidence, "action": "CLARIFY", "language": language}
                    )
                else:
                    # Store the text but ask for confirmation
                    self.redis.hset(f"conversation:{session_id}", "last_user_input", processed_text)
                    self.update_state(session_id, ConversationState.CLARIFYING)
                    return (
                        ConversationState.CLARIFYING,
                        self.confidence_router.get_clarification_message(confidence),
                        False,
                        {"confidence": confidence, "action": "CLARIFY", "text": processed_text, "language": language}
                    )
        
        else:  # ACCEPT
            # High confidence - process normally
            state, response, should_end = self.process_state_transition(session_id, processed_text)
            return state, response, should_end, {
                "confidence": confidence,
                "action": "ACCEPT",
                "language": language
            }

