from typing import Dict, Tuple
from enum import Enum


class ConfidenceAction(Enum):
    ACCEPT = "ACCEPT"      # confidence >= 0.8
    CLARIFY = "CLARIFY"   # confidence 0.2-0.8
    REJECT = "REJECT"     # confidence < 0.2


class ConfidenceRouter:
    def __init__(
        self,
        high_threshold: float = 0.8,
        low_threshold: float = 0.2  # Lowered from 0.5 to 0.2 to accept more valid transcriptions
    ):
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
    
    def route(self, asr_result: Dict) -> Tuple[ConfidenceAction, str]:
        """
        Route based on ASR confidence
        
        Returns: (action, text)
        """
        confidence = asr_result.get("confidence", 0.0)
        text = asr_result.get("text", "")
        
        if confidence >= self.high_threshold:
            # High confidence - accept input
            return ConfidenceAction.ACCEPT, text
        
        elif confidence >= self.low_threshold:
            # Medium confidence - ask for clarification
            return ConfidenceAction.CLARIFY, text
        
        else:
            # Low confidence - reject
            return ConfidenceAction.REJECT, ""
    
    def get_clarification_message(self, confidence: float) -> str:
        """Generate clarification message based on confidence"""
        if confidence >= 0.7:
            return "I think I heard you, but could you confirm that?"
        elif confidence >= 0.4:
            return "I didn't catch that clearly. Could you please repeat?"
        else:
            # For confidence 0.2-0.4, still ask for clarification but be more gentle
            return "I didn't catch that clearly. Could you please repeat?"

