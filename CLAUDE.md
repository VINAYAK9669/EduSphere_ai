# EduSphere AI Service — CLAUDE.md

> Repo: `edusphere-ai` · Deployed on: Railway (Python service)
> Part of a 3-service architecture:
>   - edusphere-frontend  (Next.js 14)        → UI layer
>   - edusphere-backend   (Node.js + Express)  → business logic, auth, proxy
>   - edusphere-ai        (FastAPI + Python)   → this repo

---

## What This Repo Does

All AI workloads for EduSphere:
- RAG pipeline: PDF ingestion (chunk + embed + store) and querying (retrieve + generate)
- Self-reflection report generation from 360° assessment data
- Streaming responses via SSE

This service is INTERNAL only. It is never called directly by the frontend.
Every request comes from `edusphere-backend` and must include `X-Internal-Secret` header.
This service has no auth of its own beyond validating that shared secret.

---

## Repo Structure

```
edusphere-ai/
├── main.py                         # FastAPI app entry point
├── app/
│   ├── routers/
│   │   ├── rag.py                  # /rag/ingest, /rag/query
│   │   └── reflection.py           # /reflection/generate, /reflection/get
│   │
│   ├── services/
│   │   ├── ingest.py               # PDF → chunks → embed → ChromaDB
│   │   ├── query.py                # Question → embed → retrieve → stream
│   │   └── reflection.py           # Assessment data → prompt → stream report
│   │
│   ├── core/
│   │   ├── config.py               # Settings via pydantic-settings (.env)
│   │   ├── security.py             # Validate X-Internal-Secret middleware
│   │   ├── chroma.py               # ChromaDB client singleton
│   │   └── claude.py               # Anthropic SDK client singleton
│   │
│   ├── schemas/
│   │   ├── rag.py                  # Pydantic request/response models
│   │   └── reflection.py
│   │
│   └── utils/
│       ├── chunker.py              # LangChain text splitter wrapper
│       └── pdf_parser.py           # pypdf text extraction
│
├── requirements.txt
├── .env
└── CLAUDE.md
```

---

## Security — Internal Secret

Every request must include the header `X-Internal-Secret`.
Validated in `app/core/security.py` as a FastAPI dependency.
Requests without a valid secret return 401 immediately.

```python
# app/core/security.py
from fastapi import Header, HTTPException, Security
from app.core.config import settings

async def verify_internal_secret(x_internal_secret: str = Header(...)):
    if x_internal_secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

Apply to every router:
```python
router = APIRouter(dependencies=[Security(verify_internal_secret)])
```

Never remove this dependency from any router. Never expose this service publicly.

---

## RAG Pipeline

### Ingest — app/services/ingest.py

Called once when teacher uploads a PDF note. Backend sends the PDF as base64 or an R2 presigned URL.

```
Input: { class_id, note_id, note_title, pdf_url }

Steps:
1. Download PDF from R2 presigned URL (httpx async)
2. Extract text → pypdf PdfReader
3. Split → LangChain RecursiveCharacterTextSplitter(
       chunk_size=500, chunk_overlap=50, length_function=len
   )
4. Embed each chunk → Anthropic embeddings (model: claude-3-haiku-20240307)
   - Batch in groups of 20 to avoid rate limits
   - Use asyncio.gather for concurrent embedding
5. Upsert to ChromaDB collection: f"notes_{class_id}"
   - Document ID: f"{note_id}_chunk_{i}"
   - Metadata: { class_id, note_id, note_title, page, chunk_index }
6. Return: { chunks_indexed: int, collection: str }
```

Collection naming: `notes_{class_id}` — one collection per class. Always.

### Query — app/services/query.py

Called on every student chat message. Returns a streaming response.

```
Input: { class_id, note_id, question, chat_history (last 6 messages) }

Steps:
1. Embed the question → Anthropic embeddings
2. ChromaDB similarity search:
   - Collection: f"notes_{class_id}"
   - Filter metadata: { note_id: note_id }  ← scoped to one note
   - n_results: 3
   - Min distance threshold: 0.3  (ChromaDB uses distance not score — lower = better)
3. If no results below threshold:
   - Stream: "I couldn't find an answer in these class notes. Please ask your teacher."
   - Return early — do NOT call Claude
4. Build system prompt (see below)
5. Call Claude API with streaming:
   - model: claude-sonnet-4-6
   - max_tokens: 1024
   - stream: True
6. Yield text chunks via FastAPI StreamingResponse
7. After stream ends: yield sources as final SSE event
   - Format: "data: [SOURCES] {json}\n\n"
   - Sources: [{ note_title, page, chunk_index }] for each chunk used
```

**System prompt (do not change without testing):**
```
You are a study assistant for EduSphere. Answer questions using ONLY the provided context from class notes.
If the context does not contain the answer, say: "I couldn't find this in the notes."
Do not use any outside knowledge. Be concise and direct.
Cite the note title and page when relevant.

Context:
{chunks}
```

### ChromaDB Client — app/core/chroma.py

```python
import chromadb
from app.core.config import settings

_client = None

def get_chroma_client():
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return _client

def get_or_create_collection(class_id: str):
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=f"notes_{class_id}",
        metadata={"hnsw:space": "cosine"}
    )
```

---

## Reflection Report — app/services/reflection.py

### When to Call
Backend only calls this endpoint after verifying both teacher and student assessments exist for the same `(student_id, class_id)`.

### Input Schema

```python
class CompetencyScore(BaseModel):
    competency_name: str
    competency_id: str
    score: float          # 1.0 – 5.0
    comment: str | None

class ReflectionInput(BaseModel):
    student_id: str
    class_id: str
    student_name: str
    teacher_scores: list[CompetencyScore]
    self_scores: list[CompetencyScore]
```

### Report Structure

Build prompt to generate 4 sections:
1. **Strengths** — both teacher ≥ 4 and self ≥ 4
2. **Blind spots** — self ≥ 4 but teacher ≤ 2 (overestimation)
3. **Hidden strengths** — teacher ≥ 4 but self ≤ 2 (underestimation)
4. **Growth areas** — both ≤ 2

Tone: encouraging, specific, coaching-style. Never generic. Address the student by first name.

```python
def build_reflection_prompt(data: ReflectionInput) -> str:
    teacher_map = {s.competency_name: s for s in data.teacher_scores}
    self_map = {s.competency_name: s for s in data.self_scores}
    # ... compute categories ...
    return f"""
You are a professional coach writing a personalised development report.
Student name: {data.student_name}

Assessment data (scale 1-5):
Teacher scores: {teacher_formatted}
Self scores: {self_formatted}

Write a coaching report with exactly these 4 sections:
1. Your Strengths
2. Blind Spots (areas you may be overestimating)
3. Hidden Strengths (areas you may be underestimating)
4. Growth Opportunities

Rules: Be specific. Use the student's name. Encourage. No generic phrases like "good job".
Each section 3-5 sentences. Do not invent scores not in the data.
"""
```

Model: `claude-sonnet-4-6`, `max_tokens: 2048`, `stream: True`.

---

## FastAPI App Structure — main.py

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from app.routers import rag, reflection
from app.core.config import settings

app = FastAPI(title="EduSphere AI", docs_url=None, redoc_url=None)  # no public docs

app.include_router(rag.router, prefix="/rag")
app.include_router(reflection.router, prefix="/reflection")

@app.get("/health")
async def health():
    return {"status": "ok"}
```

`docs_url=None` — disable Swagger UI in all environments. This is internal only.

---

## Streaming Response Pattern

All generative endpoints use `StreamingResponse`:

```python
from fastapi.responses import StreamingResponse
from anthropic import AsyncAnthropic

async def stream_claude(prompt: str, system: str):
    client = AsyncAnthropic()
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield f"data: {text}\n\n"

@router.post("/rag/query")
async def query(body: QueryInput, _=Security(verify_internal_secret)):
    # ... validation + retrieval ...
    return StreamingResponse(
        stream_claude(prompt, system_prompt),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},  # disable nginx buffering
    )
```

Always set `X-Accel-Buffering: no` — Railway uses nginx and will buffer SSE without this.

---

## Pydantic Schemas

All request bodies use Pydantic v2 models in `app/schemas/`. Validation is automatic via FastAPI.

```python
# app/schemas/rag.py
from pydantic import BaseModel

class IngestInput(BaseModel):
    class_id: str
    note_id: str
    note_title: str
    pdf_url: str             # R2 presigned URL

class QueryInput(BaseModel):
    class_id: str
    note_id: str
    question: str
    chat_history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]
```

---

## Claude API Client — app/core/claude.py

```python
from anthropic import AsyncAnthropic
from app.core.config import settings

_client = None

def get_claude_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client
```

Always import from this singleton. Never instantiate `AsyncAnthropic()` in a service file.

Models used:
- Embeddings: `claude-3-haiku-20240307` — cheapest, fast
- RAG answers + reflection: `claude-sonnet-4-6` — best balance

---

## Config — app/core/config.py

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    anthropic_api_key: str
    internal_secret: str
    chroma_host: str = "localhost"
    chroma_port: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## Environment Variables

```bash
# .env
ANTHROPIC_API_KEY=
INTERNAL_SECRET=           # must match AI_INTERNAL_SECRET in edusphere-backend

CHROMA_HOST=localhost       # ChromaDB host (Railway internal: edusphere-chroma.railway.internal)
CHROMA_PORT=8000
```

---

## Dependencies — requirements.txt

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
anthropic==0.34.0
langchain==0.3.0
langchain-text-splitters==0.3.0
chromadb==0.5.5
pypdf==4.3.1
httpx==0.27.2
pydantic-settings==2.5.2
python-multipart==0.0.12
```

Pin all versions. Do not use `>=` ranges — AI library APIs break between minor versions.

---

## Scripts

```bash
# Development
uvicorn main:app --reload --port 8000

# Production (Railway start command)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2

# Dependencies
pip install -r requirements.txt

# Type checking (optional but useful)
mypy app/ --ignore-missing-imports
```

---

## API Endpoints

| Method | Path | Called by | Description |
|---|---|---|---|
| POST | `/rag/ingest` | Backend (on note upload) | Chunk + embed + store PDF |
| POST | `/rag/query` | Backend (on chat message) | RAG query → stream answer |
| POST | `/reflection/generate` | Backend (on assessment complete) | Generate AI report → stream |
| GET | `/health` | Railway health check | Status check |

No other endpoints. Do not add CRUD endpoints — data lives in the backend.

---

## What NOT to Do

- No auth logic beyond validating `X-Internal-Secret`.
- No direct database connections to PostgreSQL or MongoDB — data is passed in the request payload by the backend.
- No business rule checks (enrollment, role) — backend handles those before calling this service.
- No public Swagger/OpenAPI docs — `docs_url=None` always.
- No answering RAG queries from Claude's general knowledge — context-only, enforced in system prompt.
- No `>=` version ranges in requirements.txt — pin everything.
- Never instantiate `AsyncAnthropic()` or `chromadb.HttpClient()` in service files — use the singletons.
- No committing `.env`.

---

## Git

```
feat: add streaming RAG query endpoint
fix: scope ChromaDB search to note_id metadata filter
chore: pin chromadb to 0.5.5
refactor: extract prompt builder to separate function
```

---

## Deployment

- Platform: **Railway** (Python service)
- Start command: `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2`
- ChromaDB: separate Railway service in the same project (accessible via internal hostname)
- Internal hostname pattern: `edusphere-ai.railway.internal:8000`
- This service is NOT exposed to the public internet — Railway private networking only

---

## Session Context Hint

When compacting, preserve:
1. Which endpoint/service is currently being built (ingest / query / reflection)
2. The ChromaDB collection naming convention in use
3. Whether streaming is wired up end-to-end yet
4. Current roadmap week (1 / 2 / 3 / 4)
