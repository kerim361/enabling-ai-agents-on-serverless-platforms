# EC2 Stateful AI Chatbot

This is the non-Lambda version of the chatbot.

## What it does

- Runs as a normal Python server on EC2
- Uses Groq directly for model responses
- Stores chat sessions locally in SQLite
- Exposes the same `/chat` API shape as the Lambda version
- Supports `action=list_sessions` for the client session picker

## Files

- `server.py` - FastAPI app with chat and session storage
- `requirements.txt` - Python dependencies
- `data/` - SQLite database and local state are stored here at runtime

## Local run

```bash
cd ec2-chatbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY="gsk_..."
export GROQ_MODEL="llama-3.3-70b-versatile"
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Use from the existing client

Set the client to the EC2 server URL:

```bash
cd client
source venv/bin/activate
export API_URL="http://YOUR_EC2_PUBLIC_IP:8000/chat"
python client.py
```

## EC2 deployment notes

1. Install Python 3.13 or 3.12 on the EC2 instance.
2. Open the security group for port 8000, or put Nginx in front of the app.
3. Keep `GROQ_API_KEY` as an environment variable on the instance.
4. Run the app with `uvicorn` directly or via `systemd`.

Example systemd service:

```ini
[Unit]
Description=EC2 Stateful AI Chatbot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/ec2-chatbot
Environment=GROQ_API_KEY=your_key_here
Environment=GROQ_MODEL=llama-3.3-70b-versatile
ExecStart=/home/ubuntu/ec2-chatbot/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
