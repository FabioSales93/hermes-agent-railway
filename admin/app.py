"""Thin Starlette wrapper in front of hermes-webui.

Adds one new surface — `/tui` — that exposes an in-browser xterm with two modes:
  - OAuth one-shots: `hermes auth add <X> --type oauth --no-browser` for Codex /
    Nous Portal device-code flows (`/tui/ws/auth/<provider>`).
  - Free-form shell: `/bin/bash -i` for users without SSH access who need to
    run other `hermes` CLI commands or peek at `/data` (`/tui/ws/shell`).

Hermes CLI ``hermes dashboard`` is reverse-proxied under ``/hermes-dashboard`` by default
(override with ``HERMES_DASHBOARD_MOUNT_PATH`` — see ``admin/dashboard_proxy.py``): loopback port
9119 (``HERMES_DASHBOARD_HOST`` / ``HERMES_DASHBOARD_PORT``) with ``X-Forwarded-Prefix`` so
upstream rewrites SPA asset URLs correctly.

Every other path is reverse-proxied to hermes-webui on loopback
(``HERMES_WEBUI_HOST`` / ``HERMES_WEBUI_PORT``, default ``127.0.0.1:9120``),
including WebSockets and SSE chat streams.

This wrapper does NOT enforce separate auth on traffic proxied to hermes-webui; that app
handles its password gate via session cookies / `/login`.

The **`/tui`** page probes hermes-webui's API cookies before responding; **`/hermes-dashboard`** runs
Hermes upstream's CLI dashboard and does **not** use **`ADMIN_PASSWORD`**. Whenever **`hermes dashboard`** is
listening it can expose **`.env`** — minimize uptime on public Railway URLs unless you acknowledge that risk (see upstream [**Web Dashboard**](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) docs).
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute

from . import proxy as hermes_proxy
from . import terminal as hermes_terminal
from .dashboard_proxy import DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app


TEMPLATE_PATH = Path(__file__).parent / "templates" / "tui.html"


# ============================================================
# Z-API Webhook Handler (RaspadinhaShow) - BOT INTELIGENTE COM LLM
# ============================================================
import os
import httpx
import json
from datetime import datetime
from typing import Dict, List

ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_INSTANCE_TOKEN = os.getenv("ZAPI_INSTANCE_TOKEN")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN")

ZAPI_BASE_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_INSTANCE_TOKEN}"
ZAPI_HEADERS = {"Client-Token": ZAPI_CLIENT_TOKEN, "Content-Type": "application/json"}

# Memória de conversa (em produção: Redis/SQLite)
conversation_memory: Dict[str, List[Dict]] = {}

# Deduplicação: messageIds já processados (evita loop de retry do Z-API)
processed_message_ids: set = set()
MAX_PROCESSED_IDS = 10000

SYSTEM_PROMPT = """Você é o assistente da RaspadinhaShow 🍓 — distribuidora de produtos para raspadinha/geladinho.

PERSONALIDADE: Amigável, direto, profissional. Respostas curtas (máx 3 frases). Uma pergunta por vez.

OBJETIVO: Agendar visitas de reposição coletando:
1. Quais produtos precisa
2. Quando prefere visita (dias/horários)  
3. Observações/restrições

FLUXO: Converse naturalmente. Extraia info conforme surgem.
Exemplo: "Copinhos e palitos semana que vem, dia 20 não tem ninguém" → "Entendido! Dia 20 não rola — dia 19 manhã ou dia 21 tarde?"

REGRAS:
- SEMPRE confirme antes de agendar: "Confirmo visita dia X às Yh, certo?"
- Se não quer reposição: "Sem problemas! Qualquer coisa é só chamar 🍓"
- Não invente datas/produtos — pergunte se não souber"""

async def send_zapi_message(phone: str, message: str) -> dict:
    url = f"{ZAPI_BASE_URL}/send-text"
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=ZAPI_HEADERS, json=payload)
    return r.json()

def extract_message_text(data: dict) -> str:
    msg_obj = data.get("message", {}) or {}
    raw_text = msg_obj.get("text", "") or data.get("text", "") or ""
    if isinstance(raw_text, dict):
        return raw_text.get("text", "").strip()
    return str(raw_text).strip()

def get_memory(phone: str) -> List[Dict]:
    if phone not in conversation_memory:
        conversation_memory[phone] = []
    return conversation_memory[phone]

def add_memory(phone: str, role: str, content: str):
    mem = get_memory(phone)
    mem.append({"role": role, "content": content, "time": datetime.now().isoformat()})
    if len(mem) > 20:
        conversation_memory[phone] = mem[-20:]

async def call_llm(messages: List[Dict]) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://inference-api.nousresearch.com/v1/chat/completions",
                json={
                    "model": "Hermes-3-Llama-3.1-70B-FP8",
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 300
                },
                headers={"Content-Type": "application/json"}
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Bot] LLM falhou: {e}")
    return intelligent_fallback(messages)

def intelligent_fallback(messages: List[Dict]) -> str:
    user_msgs = [m["content"].lower() for m in messages if m["role"] == "user"]
    last_msg = user_msgs[-1] if user_msgs else ""
    all_text = " ".join(user_msgs)
    
    is_greeting = any(p in last_msg for p in ["oi", "olá", "bom dia", "boa tarde", "boa noite", "ola"])
    said_no = any(p in last_msg for p in ["não preciso", "nao preciso", "não quero", "nao quero", "sem reposição", "não vou", "nao vou"])
    has_products = any(p in all_text for p in ["copinho", "palito", "saco", "xarope", "sabor", "copo", "colher", "tampa", "produto"])
    has_specific_date = any(p in last_msg for p in ["dia ", "amanhã", "hoje", "segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo", "manhã", "tarde", "noite"])
    has_generic_time = any(p in all_text for p in ["semana", "próxim", "proxim", "vindo"])
    confirmation = any(p in last_msg for p in ["confirmo", "confirmado", "pode confirmar", "tá bom", "ta bom", "certo", "ok", "beleza", "sim", "pode ser"])
    wants_reschedule = any(p in last_msg for p in ["reagendar", "mudar", "outro dia", "outra data", "não dá", "nao da"])
    
    if said_no and not has_products:
        return "Sem problemas! Qualquer coisa é só chamar 🍓"
    
    if (is_greeting or len(user_msgs) == 1) and not has_products:
        return ("Olá! Tudo bem? 🍓 Essa é a mensagem automática de agendamento. "
                "Você precisa de reposição de produtos esta semana?\n\n"
                "Responda:\n1️⃣ SIM - Preciso de reposição\n2️⃣ NÃO - Não preciso esta semana")
    
    if has_products and not has_specific_date:
        produtos = [p for p in ["copinho", "palito", "saco", "xarope", "sabor", "copo", "colher", "tampa"] if p in all_text]
        prod_txt = ", ".join(produtos) if produtos else "os produtos"
        return (f"Anotado: {prod_txt} ✅\n\n"
                f"Quando prefere a visita? Me avisa dia/horário que fica melhor "
                f"(ex: \"quarta de manhã\" ou \"dia 20 à tarde\").")
    
    if has_products and has_specific_date and not confirmation:
        return ("Entendido! Anotei a preferência de data.\n\n"
                "Confirmo a visita combinada — certo?\n\n"
                "Responda:\n1️⃣ SIM - Confirmo a visita\n2️⃣ NÃO - Preciso ajustar")
    
    if confirmation and has_products and has_specific_date:
        return ("✅ Visita confirmada! 🍓\n\n"
                "Nossa equipe vai te visitar no dia combinado. Se houver imprevisto, avisa a gente.\n\n"
                "Obrigado! 🍓 *RaspadinhaShow*")
    
    if wants_reschedule and has_products:
        return ("📅 Sem problemas!\n\nMe avisa qual dia/horário fica melhor pra você. "
                "A gente ajusta e confirma.")
    
    if last_msg in ["sim", "s", "1", "quero", "preciso"] and not has_products:
        return ("Beleza! Quais produtos você precisa? "
                "(ex: copinhos, palitos, sacos, xaropes, sabores...)")
    
    return ("Não entendi completamente. Me explica melhor: "
            "quais produtos precisa e quando prefere a visita?")

async def process_message(data: dict) -> dict:
    phone = data.get("phone") or data.get("from") or data.get("sender", {}).get("phone")
    message_text = extract_message_text(data)
    message_id = data.get("messageId") or data.get("id") or data.get("message", {}).get("id")
    event_type = data.get("type") or data.get("event")
    
    # Ignora status updates (delivered, read, etc.) - não têm texto de mensagem
    if event_type in ["status", "ack", "delivery", "read", "received"] and not message_text:
        return {"status": "ignored", "reason": f"status event: {event_type}"}
    
    # Ignora se não tem telefone ou mensagem
    if not phone or not message_text:
        return {"status": "ignored", "reason": "missing phone or message"}
    
    # Deduplicação por messageId
    if message_id:
        if message_id in processed_message_ids:
            return {"status": "ignored", "reason": f"duplicate messageId: {message_id}"}
        processed_message_ids.add(message_id)
        # Limita tamanho do set
        if len(processed_message_ids) > MAX_PROCESSED_IDS:
            # Remove metade dos mais antigos (simples)
            to_remove = list(processed_message_ids)[:MAX_PROCESSED_IDS // 2]
            for mid in to_remove:
                processed_message_ids.discard(mid)
    
    add_memory(phone, "user", message_text)
    
    memory = get_memory(phone)
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    llm_messages.extend(memory[-10:])
    
    bot_response = await call_llm(llm_messages)
    add_memory(phone, "assistant", bot_response)
    
    if bot_response:
        await send_zapi_message(phone, bot_response)
    
    return {"status": "processed", "phone": phone, "preview": bot_response[:80] + "..."}

async def raspadinha_webhook_handler(data: dict) -> dict:
    return await process_message(data)


async def zapi_webhook(request: Request):
    """Endpoint do webhook Z-API"""
    try:
        data = await request.json()
        print(f"[ZAPI Webhook] RAW PAYLOAD: {json.dumps(data, ensure_ascii=False, indent=2)}")
        print(f"[ZAPI Webhook] Headers: {dict(request.headers)}")
        
        event_type = data.get("type") or data.get("event")
        
        # Z-API manda "ReceivedCallback" para mensagens recebidas
        if event_type in ["message", "receive", "text", "incoming", "ReceivedCallback"] or "message" in data:
            result = await raspadinha_webhook_handler(data)
            return JSONResponse({"received": True, "result": result})
        
        return JSONResponse({"received": True, "event": event_type})
        
    except Exception as e:
        print(f"[ZAPI Webhook] Erro: {e}")
        return JSONResponse({"received": False, "error": str(e)}, status_code=500)


async def zapi_webhook_health(request: Request):
    """Health check do webhook"""
    return JSONResponse({
        "status": "ok",
        "service": "raspadinhashow-webhook",
        "instance": ZAPI_INSTANCE_ID
    })


async def _is_authenticated(request: Request) -> bool:
    """Cheap auth probe: hit hermes-webui's /api/onboarding/status with the user's cookies.

    First call after boot can be slow (5+s) due to hermes_cli imports inside the
    webui server. Use a generous timeout — auth checks are infrequent.
    """
    import httpx

    cookie = request.headers.get("cookie", "")
    if not cookie:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{hermes_proxy.WEBUI_BASE_URL}/api/onboarding/status",
                headers={"cookie": cookie, "host": f"{hermes_proxy.WEBUI_HOST}:{hermes_proxy.WEBUI_PORT}"},
            )
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


async def tui_page(request: Request):
    if not await _is_authenticated(request):
        return RedirectResponse("/login?next=/tui", status_code=303)
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))


routes = [
    Route("/tui", tui_page, methods=["GET"]),
    WebSocketRoute("/tui/ws/auth/{provider}", hermes_terminal.login_ws),
    WebSocketRoute("/tui/ws/shell", hermes_terminal.shell_ws),
    Mount(DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app()),
    # Z-API Webhook endpoints (RaspadinhaShow)
    Route("/webhook/zapi", zapi_webhook, methods=["POST"]),
    Route("/webhook/zapi/health", zapi_webhook_health, methods=["GET"]),
    # Catch-all proxy for everything else (HTTP + WebSocket).
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    # Root path needs its own route — Starlette's path converter requires at least one segment.
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]


app = Starlette(debug=False, routes=routes)