# Redis Setup Guide

## Quick Start

### Option 1: Docker (Recommended)
```bash
docker run -d -p 6379:6379 --name redis-voicebot redis:latest
```

### Option 2: Windows (WSL)
```bash
# In WSL
sudo apt-get update
sudo apt-get install redis-server
redis-server
```

### Option 3: Windows Native
Download Redis for Windows from: https://github.com/microsoftarchive/redis/releases

## Verify Redis is Running

```bash
# Test connection
redis-cli ping
# Should return: PONG
```

## Environment Variables (Optional)

Create a `.env` file with:
```
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
IDLE_TIMEOUT=30
MAX_SESSION_DURATION=600
```

## Session States

- **NEW**: Session just created
- **ACTIVE**: User and bot actively talking
- **IDLE**: No activity for 30 seconds (configurable)
- **CLOSED**: Session ended or timeout

## Notes

- The app will work without Redis but without session management
- Sessions are automatically cleaned up after timeout
- Session data is kept for 24 hours after closing for analytics

