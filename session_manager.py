import redis
import json
import uuid
from datetime import datetime
from typing import Optional, Dict, List
from enum import Enum


class SessionState(Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    CLOSED = "CLOSED"


class SessionManager:
    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        idle_timeout: int = 30,
        max_session_duration: int = 600
    ):
        try:
            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Test connection
            self.redis_client.ping()
        except redis.ConnectionError as e:
            raise RuntimeError(f"Failed to connect to Redis: {e}")
        
        self.idle_timeout = idle_timeout
        self.max_session_duration = max_session_duration
    
    def create_session(self, user_id: Optional[str] = None) -> str:
        """Create a new session and return session_id"""
        session_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        session_data = {
            "session_id": session_id,
            "state": SessionState.NEW.value,
            "created_at": now,
            "last_activity": now,
            "idle_timeout": str(self.idle_timeout),
            "max_duration": str(self.max_session_duration),
            "user_id": user_id or "",
            "metadata": "{}"
        }
        
        # Store session
        self.redis_client.hset(f"session:{session_id}", mapping=session_data)
        
        # Add to active sessions set
        self.redis_client.sadd("sessions:active", session_id)
        
        # Set expiration for automatic cleanup
        self.redis_client.expire(f"session:{session_id}", self.max_session_duration + 60)
        
        return session_id
    
    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session data"""
        data = self.redis_client.hgetall(f"session:{session_id}")
        return data if data else None
    
    def update_state(self, session_id: str, new_state: SessionState):
        """Update session state"""
        self.redis_client.hset(f"session:{session_id}", "state", new_state.value)
        
        if new_state == SessionState.CLOSED:
            self.redis_client.srem("sessions:active", session_id)
    
    def update_activity(self, session_id: str):
        """Update last activity timestamp and transition to ACTIVE"""
        now = datetime.utcnow().isoformat()
        current_state = self.get_session_state(session_id)
        
        if current_state is None:
            return False
        
        # Update last activity
        self.redis_client.hset(f"session:{session_id}", "last_activity", now)
        
        # Transition from NEW/IDLE to ACTIVE
        if current_state in [SessionState.NEW, SessionState.IDLE]:
            self.update_state(session_id, SessionState.ACTIVE)
        
        return True
    
    def get_session_state(self, session_id: str) -> Optional[SessionState]:
        """Get current session state"""
        state_str = self.redis_client.hget(f"session:{session_id}", "state")
        if not state_str:
            return None
        try:
            return SessionState(state_str)
        except ValueError:
            return None
    
    def check_idle(self, session_id: str) -> bool:
        """Check if session should be marked as IDLE"""
        session = self.get_session(session_id)
        if not session:
            return False
        
        try:
            last_activity = datetime.fromisoformat(session["last_activity"])
            idle_seconds = (datetime.utcnow() - last_activity).total_seconds()
            return idle_seconds >= self.idle_timeout
        except (KeyError, ValueError):
            return False
    
    def check_timeout(self, session_id: str) -> bool:
        """Check if session has exceeded max duration"""
        session = self.get_session(session_id)
        if not session:
            return False
        
        try:
            created_at = datetime.fromisoformat(session["created_at"])
            duration = (datetime.utcnow() - created_at).total_seconds()
            return duration >= self.max_session_duration
        except (KeyError, ValueError):
            return False
    
    def add_to_history(self, session_id: str, role: str, content: str):
        """Add message to chat history"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.redis_client.lpush(f"session:{session_id}:history", json.dumps(message))
        # Keep last 50 messages
        self.redis_client.ltrim(f"session:{session_id}:history", 0, 49)
    
    def get_history(self, session_id: str, limit: int = 20) -> List[Dict]:
        """Get chat history"""
        messages = self.redis_client.lrange(f"session:{session_id}:history", 0, limit - 1)
        history = []
        for msg in reversed(messages):
            try:
                history.append(json.loads(msg))
            except json.JSONDecodeError:
                continue
        return history
    
    def close_session(self, session_id: str):
        """Explicitly close a session"""
        self.update_state(session_id, SessionState.CLOSED)
        # Keep session data for 24 hours for analytics
        self.redis_client.expire(f"session:{session_id}", 86400)
        self.redis_client.expire(f"session:{session_id}:history", 86400)
    
    def cleanup_idle_sessions(self):
        """Background task to mark idle sessions"""
        try:
            active_sessions = self.redis_client.smembers("sessions:active")
            
            for session_id in active_sessions:
                if self.check_idle(session_id):
                    current_state = self.get_session_state(session_id)
                    if current_state == SessionState.ACTIVE:
                        self.update_state(session_id, SessionState.IDLE)
                
                if self.check_timeout(session_id):
                    self.close_session(session_id)
        except Exception as e:
            print(f"Error in cleanup_idle_sessions: {e}")

