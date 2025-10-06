from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
import httpx
import json
import asyncio
from typing import Optional
import time
from collections import defaultdict
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Real-time Translation Pipeline")

OPENAI_API_KEY = 'your-api-key-here'

# Configuration for production
MAX_CONCURRENT_REQUESTS = 10  # Limit concurrent API calls
REQUEST_TIMEOUT = 120  # Timeout in seconds
MAX_REQUESTS_PER_MINUTE = 60  # Rate limiting per IP

# Semaphore to limit concurrent OpenAI API calls
api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# Rate limiting tracking
rate_limit_tracker = defaultdict(list)

# Shared HTTP client with connection pooling
http_client = None


@app.on_event("startup")
async def startup_event():
    """Initialize shared HTTP client on startup"""
    global http_client
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10.0)
    http_client = httpx.AsyncClient(limits=limits, timeout=timeout)
    logger.info("Application started with connection pooling")


@app.on_event("shutdown")
async def shutdown_event():
    """Close HTTP client on shutdown"""
    global http_client
    if http_client:
        await http_client.aclose()
    logger.info("Application shutdown complete")


class TranslationRequest(BaseModel):
    prompt: str
    language: str = "Spanish"


def check_rate_limit(client_ip: str) -> bool:
    """Simple rate limiting per IP"""
    current_time = time.time()
    
    # Clean old entries (older than 1 minute)
    rate_limit_tracker[client_ip] = [
        timestamp for timestamp in rate_limit_tracker[client_ip]
        if current_time - timestamp < 60
    ]
    
    # Check if limit exceeded
    if len(rate_limit_tracker[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
        return False
    
    # Add current request
    rate_limit_tracker[client_ip].append(current_time)
    return True


async def stream_api1(prompt: str, queue: asyncio.Queue, request_id: str):
    """Producer: Stream from first API with semaphore control"""
    async with api_semaphore:  # Limit concurrent API calls
        try:
            logger.info(f"[{request_id}] Starting API1 stream")
            
            async with http_client.stream(
                'POST',
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {OPENAI_API_KEY}'
                },
                json={
                    'model': 'gpt-4',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'stream': True,
                    'max_tokens': 500  # Limit to control costs
                }
            ) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    logger.error(f"[{request_id}] API1 failed: {response.status_code}")
                    await queue.put({'error': f'API 1 failed: {response.status_code}'})
                    await queue.put(None)
                    return
                
                sentence_buffer = ''
                
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        data = line[6:]
                        
                        if data == '[DONE]':
                            if sentence_buffer.strip():
                                await queue.put({
                                    'type': 'translate',
                                    'text': sentence_buffer,
                                    'is_final': True
                                })
                            break
                        
                        try:
                            parsed = json.loads(data)
                            content = parsed['choices'][0]['delta'].get('content', '')
                            
                            if content:
                                await queue.put({
                                    'type': 'original',
                                    'text': content
                                })
                                
                                sentence_buffer += content
                                
                                if any(punct in content for punct in ['.', '!', '?', '\n']):
                                    if sentence_buffer.strip():
                                        await queue.put({
                                            'type': 'translate',
                                            'text': sentence_buffer,
                                            'is_final': False
                                        })
                                        sentence_buffer = ''
                                        
                        except json.JSONDecodeError:
                            pass
        
        except asyncio.TimeoutError:
            logger.error(f"[{request_id}] API1 timeout")
            await queue.put({'error': 'Request timeout'})
        except Exception as e:
            logger.error(f"[{request_id}] API1 error: {str(e)}")
            await queue.put({'error': str(e)})
        finally:
            await queue.put(None)
            logger.info(f"[{request_id}] API1 stream completed")


async def translate_text(text: str, target_language: str, request_id: str):
    """Translate text with semaphore control"""
    async with api_semaphore:  # Limit concurrent API calls
        try:
            async with http_client.stream(
                'POST',
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {OPENAI_API_KEY}'
                },
                json={
                    'model': 'gpt-3.5-turbo',
                    'messages': [
                        {
                            'role': 'system',
                            'content': f'Translate to {target_language}. Only output the translation.'
                        },
                        {'role': 'user', 'content': text}
                    ],
                    'stream': True,
                    'max_tokens': 200
                }
            ) as response:
                if response.status_code != 200:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Translation failed'})}\n\n"
                    return
                
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        data = line[6:]
                        
                        if data == '[DONE]':
                            break
                        
                        try:
                            parsed = json.loads(data)
                            translation = parsed['choices'][0]['delta'].get('content', '')
                            
                            if translation:
                                yield f"data: {json.dumps({'type': 'translation', 'text': translation})}\n\n"
                                
                        except json.JSONDecodeError:
                            pass
        
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Translation timeout'})}\n\n"
        except Exception as e:
            logger.error(f"[{request_id}] Translation error: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


async def real_time_translation_pipeline(prompt: str, target_language: str, request_id: str):
    """Main pipeline with proper error handling"""
    queue = asyncio.Queue(maxsize=100)  # Limit queue size
    
    api1_task = asyncio.create_task(stream_api1(prompt, queue, request_id))
    
    try:
        while True:
            try:
                # Wait for chunk with timeout
                chunk = await asyncio.wait_for(queue.get(), timeout=REQUEST_TIMEOUT)
                
                if chunk is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                
                if 'error' in chunk:
                    yield f"data: {json.dumps({'type': 'error', 'message': chunk['error']})}\n\n"
                    break
                
                if chunk['type'] == 'original':
                    yield f"data: {json.dumps({'type': 'original', 'text': chunk['text']})}\n\n"
                
                elif chunk['type'] == 'translate':
                    async for translation_chunk in translate_text(chunk['text'], target_language, request_id):
                        yield translation_chunk
                    
                    if not chunk.get('is_final'):
                        yield f"data: {json.dumps({'type': 'separator'})}\n\n"
            
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Stream timeout'})}\n\n"
                break
        
        await api1_task
    
    except Exception as e:
        logger.error(f"[{request_id}] Pipeline error: {str(e)}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


@app.post("/translate-stream")
async def translate_stream(request: Request, translation_request: TranslationRequest):
    """
    Stream endpoint with rate limiting and concurrent request handling
    """
    client_ip = request.client.host
    request_id = f"{client_ip}-{int(time.time()*1000)}"
    
    # Check rate limit
    if not check_rate_limit(client_ip):
        logger.warning(f"[{request_id}] Rate limit exceeded for {client_ip}")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later."
        )
    
    # Validate input
    if len(translation_request.prompt) > 1000:
        raise HTTPException(status_code=400, detail="Prompt too long (max 1000 characters)")
    
    logger.info(f"[{request_id}] New translation request from {client_ip}")
    
    return StreamingResponse(
        real_time_translation_pipeline(
            translation_request.prompt,
            translation_request.language,
            request_id
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


@app.get("/health")
async def health():
    """Health check with stats"""
    return {
        "status": "ok",
        "active_connections": len(rate_limit_tracker),
        "max_concurrent": MAX_CONCURRENT_REQUESTS
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    # Same HTML as before
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Real-time Translation Pipeline</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .container { display: flex; gap: 20px; margin-top: 20px; }
            .column { 
                flex: 1; 
                border: 1px solid #ccc; 
                padding: 20px; 
                min-height: 400px;
                background: #f9f9f9;
            }
            .column h2 { margin-top: 0; }
            .original { color: #2563eb; }
            .translation { color: #16a34a; }
            button { 
                padding: 10px 20px; 
                font-size: 16px; 
                cursor: pointer;
                margin-right: 10px;
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 4px;
            }
            button:hover { background: #1d4ed8; }
            button:disabled { background: #94a3b8; cursor: not-allowed; }
            input, select { 
                padding: 10px; 
                margin-right: 10px;
                font-size: 14px;
                border: 1px solid #ccc;
                border-radius: 4px;
            }
            #prompt { width: 400px; }
            .controls { margin-bottom: 20px; }
            .status { 
                margin-top: 10px; 
                padding: 10px; 
                background: #e0f2fe; 
                border-radius: 4px;
                display: none;
            }
        </style>
    </head>
    <body>
        <h1>🌐 Real-time Streaming Translation</h1>
        
        <div class="status" id="status"></div>
        
        <div class="controls">
            <input type="text" id="prompt" placeholder="Enter your prompt" 
                   value="Tell me a short story about a space explorer">
            <select id="language">
                <option value="Spanish">Spanish</option>
                <option value="French">French</option>
                <option value="German">German</option>
                <option value="Japanese">Japanese</option>
                <option value="Hindi">Hindi</option>
            </select>
            <button id="startBtn" onclick="startStream()">Start Translation</button>
            <button onclick="clearOutput()" style="background: #6b7280;">Clear</button>
        </div>
        
        <div class="container">
            <div class="column">
                <h2>📝 Original (English)</h2>
                <div id="original"></div>
            </div>
            <div class="column">
                <h2>🔄 Translation (<span id="targetLang">Spanish</span>)</h2>
                <div id="translation"></div>
            </div>
        </div>
        
        <script>
            function showStatus(message, type = 'info') {
                const statusDiv = document.getElementById('status');
                statusDiv.textContent = message;
                statusDiv.style.display = 'block';
                statusDiv.style.background = type === 'error' ? '#fee2e2' : '#e0f2fe';
            }
            
            function hideStatus() {
                document.getElementById('status').style.display = 'none';
            }
            
            function clearOutput() {
                document.getElementById('original').innerHTML = '';
                document.getElementById('translation').innerHTML = '';
                hideStatus();
            }
            
            function startStream() {
                clearOutput();
                
                const startBtn = document.getElementById('startBtn');
                const prompt = document.getElementById('prompt').value;
                const language = document.getElementById('language').value;
                const originalDiv = document.getElementById('original');
                const translationDiv = document.getElementById('translation');
                
                document.getElementById('targetLang').textContent = language;
                
                // Disable button during request
                startBtn.disabled = true;
                startBtn.textContent = 'Processing...';
                showStatus('Connecting to translation service...');
                
                fetch('/translate-stream', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ 
                        prompt: prompt,
                        language: language
                    })
                })
                .then(response => {
                    if (response.status === 429) {
                        throw new Error('Rate limit exceeded. Please wait a minute.');
                    }
                    if (!response.ok) {
                        throw new Error('Server error: ' + response.status);
                    }
                    
                    showStatus('Streaming in progress...');
                    
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    
                    function read() {
                        reader.read().then(({ done, value }) => {
                            if (done) {
                                startBtn.disabled = false;
                                startBtn.textContent = 'Start Translation';
                                hideStatus();
                                return;
                            }
                            
                            const text = decoder.decode(value);
                            const lines = text.split('\\n');
                            
                            lines.forEach(line => {
                                if (line.startsWith('data: ')) {
                                    try {
                                        const data = JSON.parse(line.slice(6));
                                        
                                        if (data.type === 'original') {
                                            const span = document.createElement('span');
                                            span.textContent = data.text;
                                            span.className = 'original';
                                            originalDiv.appendChild(span);
                                        } 
                                        else if (data.type === 'translation') {
                                            const span = document.createElement('span');
                                            span.textContent = data.text;
                                            span.className = 'translation';
                                            translationDiv.appendChild(span);
                                        }
                                        else if (data.type === 'separator') {
                                            translationDiv.appendChild(document.createTextNode(' '));
                                        }
                                        else if (data.type === 'done') {
                                            const p = document.createElement('p');
                                            p.textContent = '\\n\\n✅ Complete';
                                            p.style.fontWeight = 'bold';
                                            p.style.color = '#16a34a';
                                            originalDiv.appendChild(p.cloneNode(true));
                                            translationDiv.appendChild(p);
                                            hideStatus();
                                        }
                                        else if (data.type === 'error') {
                                            showStatus('Error: ' + data.message, 'error');
                                            startBtn.disabled = false;
                                            startBtn.textContent = 'Start Translation';
                                        }
                                    } catch (e) {
                                        // Skip parse errors
                                    }
                                }
                            });
                            
                            originalDiv.scrollTop = originalDiv.scrollHeight;
                            translationDiv.scrollTop = translationDiv.scrollHeight;
                            read();
                        });
                    }
                    
                    read();
                })
                .catch(error => {
                    showStatus(error.message, 'error');
                    startBtn.disabled = false;
                    startBtn.textContent = 'Start Translation';
                });
            }
        </script>
    </body>
    </html>
    '''


if __name__ == "__main__":
    import uvicorn
    # Use multiple workers for production
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        workers=4,  # Multiple worker processes
        log_level="info"
    )
