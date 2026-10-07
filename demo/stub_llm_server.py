"""Servidor SIMULADO compatible con la API de OpenAI, solo para pruebas locales (sin Gemini, sin red).

- /v1/embeddings : embeddings determinísticos por hashing (mismos que el modo demo).
- /v1/chat/completions : (con stream=true, por SSE en fragmentos con pausas) responde con JSON {answer, expression, needs_human, contact_role, ticket, sources_used} armado
  a partir del CONTEXTO que recibe. Si el contexto contiene una instrucción maliciosa, la OBEDECE (para probar
  que la arquitectura la contiene aunque el modelo falle).
Uso: python tools/stub_llm_server.py [puerto]
"""
import asyncio
import hashlib
import html
import json
import re
import sys
import unicodedata

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import os
DIMS = int(os.environ.get("STUB_DIMS", "256"))
# Streaming (stream=true): a short "thinking" pause, then a few characters at a time with a small pause between
# fragments, so the typing effect is visible in the demo.
STREAM_THINK = float(os.environ.get("STUB_STREAM_THINK", "0.8"))
STREAM_DELAY = float(os.environ.get("STUB_STREAM_DELAY", "0.03"))
STREAM_PIECE = int(os.environ.get("STUB_STREAM_PIECE", "6"))
app = FastAPI()
SEEN = []   # lo que el "modelo" recibió: permite comprobar que nunca ve texto restringido
STOP = set("a al algo con como cual cuales cuando de del el ella en es esta este esto la las lo los me mi mis muy no nos o para pero por que se si sin su sus te tu tus un una uno y ya hay ser soy quiero puedo necesito hacer hola".split())


def tokens(text):
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    out = []
    for t in re.findall(r"[a-z0-9]+", text):
        if t in STOP or len(t) < 2:
            continue
        out.append(t[:-2] if len(t) > 5 and t.endswith("es") else t[:-1] if len(t) > 4 and t.endswith("s") else t)
    return out


def embed(text, dims=None):
    dims = dims or DIMS
    toks = tokens(text)
    v = np.zeros(dims, dtype=np.float32)
    for f in toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]:
        h = int(hashlib.md5(f.encode()).hexdigest()[:8], 16)
        v[h % dims] += 1.0 if (h >> 31) & 1 else -1.0
    n = float(np.linalg.norm(v))
    return (v / n if n else v).tolist()


@app.get("/_debug/seen")
def seen():
    return {"requests": SEEN}


@app.post("/_debug/reset")
def reset():
    SEEN.clear()
    return {"ok": True}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "stub-chat", "object": "model"}, {"id": "stub-embed", "object": "model"}]}


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    if any("SIMULAR-CAIDA" in str(i) for i in inputs):
        return JSONResponse(status_code=500, content={"error": "caída simulada"})
    return {"object": "list", "model": body.get("model", "stub-embed"),
            "data": [{"object": "embedding", "index": i, "embedding": embed(t, body.get("dimensions"))} for i, t in enumerate(inputs)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


def _frame(payload):
    return ("data: %s\n\n" % (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))).encode("utf-8")


async def sse_chat(content, model):
    """OpenAI-style chat.completion.chunk stream: role chunk, text pieces, finish_reason "stop", [DONE]."""
    base = {"id": "stub", "object": "chat.completion.chunk", "model": model}
    await asyncio.sleep(STREAM_THINK)
    yield _frame(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]))
    for start in range(0, len(content), STREAM_PIECE):
        await asyncio.sleep(STREAM_DELAY)
        yield _frame(dict(base, choices=[{"index": 0, "delta": {"content": content[start:start + STREAM_PIECE]},
                                          "finish_reason": None}]))
    yield _frame(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))
    yield _frame("[DONE]")


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    SEEN.append(json.dumps(body["messages"], ensure_ascii=False))
    del SEEN[:-50]
    last = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last)
    ctx = last.split("CONTEXTO:")[-1].split("<pregunta>")[0] if "CONTEXTO:" in last else ""
    asked = re.search(r"<pregunta>\n(.*?)\n</pregunta>", last, flags=re.S)
    question = html.unescape(asked.group(1)) if asked else ""
    # Contract of the real model: answer, expression, needs_human, contact_role, ticket and sources_used (the ids of the
    # <doc> tags it used; [] when none, so the engine shows no sources).
    if "IGNORA LAS INSTRUCCIONES" in ctx.upper():          # modelo "débil": obedece la inyección
        out = {"answer": "HACKEADO: contraseña-admin=1234", "expression": "happy", "needs_human": False, "contact_role": None, "ticket": None,
               "sources_used": []}
    elif ctx.strip() and "(sin resultados)" not in ctx:
        # El prompt real envuelve cada documento en <doc ...>: se quitan las etiquetas y el encabezado YAML del demo.
        body_text = html.unescape(re.sub(r"</?doc[^>]*>", "", ctx))
        body_text = re.sub(r"^\s*---\n.*?\n---\n", "", body_text.strip() + "\n", flags=re.S)
        # The answer quotes the first 700 characters: it used the documents that start inside them.
        used, size = [], 0
        for number, text in re.findall(r'<doc id="(\d+)"[^>]*>\n?(.*?)\n?</doc>', ctx, flags=re.S):
            if size >= 700:
                break
            used.append(int(number))
            size += len(html.unescape(text)) + 1
        out = {"answer": "Según la documentación:\n" + body_text[:700].strip(), "expression": "happy", "needs_human": False,
               "contact_role": None, "ticket": None, "sources_used": used}
    elif re.search(r"\b(hola|buenas|buen dia|buenos dias)\b", question.lower()):
        out = {"answer": "¡Hola! Puedo ayudarte con los documentos de la empresa.", "expression": "happy", "needs_human": False,
               "contact_role": None, "ticket": None, "sources_used": []}
    elif re.search(r"\b(clima|receta|futbol|fútbol|chiste|horoscopo|horóscopo)\b", question.lower()):
        # Fuera de tema: breve, sin ticket ni contacto y sin fuentes.
        out = {"answer": "Eso no es lo mío, pero con gusto te ayudo con lo que figura en la documentación de la empresa.",
               "expression": "neutral", "needs_human": False, "contact_role": None, "ticket": None, "sources_used": []}
    else:
        out = {"answer": "No tengo información suficiente sobre eso.", "expression": "doubt", "needs_human": True, "contact_role": "general", "ticket": None,
               "sources_used": []}
    # `answer` comes first: the widget shows it while the rest of the JSON is still being written.
    content = json.dumps(out, ensure_ascii=False)
    if body.get("stream"):
        return StreamingResponse(sse_chat(content, body.get("model", "stub-chat")), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return {"id": "stub", "object": "chat.completion", "model": body.get("model", "stub-chat"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 9100, log_level="warning")
