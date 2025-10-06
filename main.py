from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
import httpx
import json
import asyncio
from typing import Optional

app = FastAPI(title="Real-time Translation Pipeline")

OPENAI_API_KEY = 'enter-your-key'


class TranslationRequest(BaseModel):
    prompt: str
    language: str = "Hindi"


async def stream_api1(prompt: str, queue: asyncio.Queue):
    """Producer: Stream from first API and put chunks in queue"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                'POST',
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {OPENAI_API_KEY}'
                },
                json={
                    'model': 'gpt-4',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'stream': True
                }
            ) as response:
                if response.status_code != 200:
                    await queue.put({'error': f'API 1 failed: {response.status_code}'})
                    await queue.put(None)
                    return
                
                sentence_buffer = ''
                
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        data = line[6:]
                        
                        if data == '[DONE]':
                            # Send remaining buffer
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
                                # Send original text immediately
                                await queue.put({
                                    'type': 'original',
                                    'text': content
                                })
                                
                                sentence_buffer += content
                                
                                # When we have a complete sentence, send for translation
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
    
    except Exception as e:
        await queue.put({'error': str(e)})
    finally:
        await queue.put(None)  # Signal completion


async def translate_text(text: str, target_language: str):
    """Translate text using streaming API"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
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
                    'stream': True
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
    
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


async def real_time_translation_pipeline(prompt: str, target_language: str):
    """Main pipeline that coordinates both APIs"""
    
    queue = asyncio.Queue()
    
    # Start API1 in background task
    api1_task = asyncio.create_task(stream_api1(prompt, queue))
    
    try:
        while True:
            chunk = await queue.get()
            
            if chunk is None:  # End signal
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            
            if 'error' in chunk:
                yield f"data: {json.dumps({'type': 'error', 'message': chunk['error']})}\n\n"
                continue
            
            if chunk['type'] == 'original':
                # Stream original text immediately
                yield f"data: {json.dumps({'type': 'original', 'text': chunk['text']})}\n\n"
            
            elif chunk['type'] == 'translate':
                # Stream translation
                async for translation_chunk in translate_text(chunk['text'], target_language):
                    yield translation_chunk
                
                # Add separator after each sentence
                if not chunk.get('is_final'):
                    yield f"data: {json.dumps({'type': 'separator'})}\n\n"
        
        await api1_task
    
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


@app.post("/translate-stream")
async def translate_stream(request: TranslationRequest):
    """
    Stream endpoint for real-time translation
    
    Example curl:
    curl -N -X POST http://localhost:8000/translate-stream \
      -H "Content-Type: application/json" \
      -d '{"prompt": "Tell me a story about a robot", "language": "Hindi"}'
    """
    return StreamingResponse(
        real_time_translation_pipeline(request.prompt, request.language),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
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
            input, select { 
                padding: 10px; 
                margin-right: 10px;
                font-size: 14px;
                border: 1px solid #ccc;
                border-radius: 4px;
            }
            #prompt { width: 400px; }
            .controls { margin-bottom: 20px; }
        </style>
    </head>
    <body>
        <h1>🌐 Real-time Streaming Translation</h1>
        
        <div class="controls">
            <input type="text" id="prompt" placeholder="Enter your prompt" 
                   value="Tell me a short story about a space explorer">
            <select id="language">
                <option value="Spanish">Spanish</option>
                <option value="French">French</option>
                <option value="German">German</option>
                <option value="Japanese">Japanese</option>
                <option value="Hindi">Hindi</option>
                <option value="Chinese">Chinese</option>
            </select>
            <button onclick="startStream()">Start Translation</button>
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
            function clearOutput() {
                document.getElementById('original').innerHTML = '';
                document.getElementById('translation').innerHTML = '';
            }
            
            function startStream() {
                clearOutput();
                
                const prompt = document.getElementById('prompt').value;
                const language = document.getElementById('language').value;
                const originalDiv = document.getElementById('original');
                const translationDiv = document.getElementById('translation');
                
                document.getElementById('targetLang').textContent = language;
                
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
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    
                    function read() {
                        reader.read().then(({ done, value }) => {
                            if (done) return;
                            
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
                                        }
                                        else if (data.type === 'error') {
                                            alert('❌ Error: ' + data.message);
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
                    alert('❌ Error: ' + error.message);
                });
            }
        </script>
    </body>
    </html>
    '''


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
