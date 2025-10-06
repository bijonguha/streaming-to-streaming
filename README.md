# 🌐 Real-time Streaming Translation Pipeline

A FastAPI application that chains two streaming REST APIs to provide real-time translation. The first API generates text, and the second API translates it sentence-by-sentence as it streams - no waiting for the entire text to complete!

## ✨ Features

- **Real-time Streaming**: See original text and translations appear simultaneously
- **No Buffering Wait**: Translates sentence-by-sentence as content streams
- **Multiple Languages**: Support for Spanish, French, German, Japanese, Hindi, Chinese, and more
- **Async/Await**: Built with FastAPI and httpx for optimal performance
- **Clean Web UI**: Interactive interface to test the streaming pipeline
- **Server-Sent Events (SSE)**: Efficient streaming protocol

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- OpenAI API Key

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/streaming-translation-pipeline.git
cd streaming-translation-pipeline

# Install dependencies
pip install -r requirements.txt

# Set your OpenAI API key in the app.py file
# OPENAI_API_KEY = 'your-api-key-here'
```

### Run the Server

```bash
python app.py
```

Or with uvicorn:

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000` in your browser to use the web interface.

## 📡 API Usage

### Endpoint

**POST** `/translate-stream`

### Request Body

```json
{
  "prompt": "Tell me a story about a robot",
  "language": "Spanish"
}
```

### cURL Example

```bash
curl -N -X POST http://localhost:8000/translate-stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Tell me a story about a robot", "language": "Spanish"}'
```

**Note**: The `-N` flag is essential for streaming!

### Response Format

Server-Sent Events (SSE) stream:

```
data: {"type":"original","text":"Once upon a time"}
data: {"type":"translation","text":"Había una vez"}
data: {"type":"done"}
```

## 🏗️ Architecture

```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│   API 1     │──────▶│   Queue     │──────▶│   API 2     │
│  (Generate) │       │ (Async)     │       │ (Translate) │
└─────────────┘       └─────────────┘       └─────────────┘
       │                                            │
       ▼                                            ▼
   Stream to Client                         Stream to Client
   (Original Text)                          (Translation)
```

1. **API 1** streams text generation in real-time
2. **Queue** buffers until sentence completion (`.`, `!`, `?`)
3. **API 2** translates each sentence immediately
4. **Client** receives both streams in parallel

## 🛠️ Technology Stack

- **FastAPI**: Modern async web framework
- **httpx**: Async HTTP client for streaming
- **OpenAI API**: Text generation and translation
- **Server-Sent Events**: Efficient streaming protocol

## 📝 Supported Languages

- Spanish
- French
- German
- Japanese
- Hindi
- Chinese
- And more! (easily extendable)

## 🔧 Configuration

Edit `app.py` to customize:

- `OPENAI_API_KEY`: Your OpenAI API key
- Model selection: Change `gpt-4` or `gpt-3.5-turbo` to other models
- Port: Default is 8000

## 📄 License

MIT License - feel free to use this project however you'd like!

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## ⚠️ Note

Remember to keep your OpenAI API key secure and never commit it to version control. Consider using environment variables:

```python
import os
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
```

## 📞 Support

If you encounter any issues or have questions, please open an issue on GitHub.

---

Made with ❤️ using FastAPI and OpenAI
