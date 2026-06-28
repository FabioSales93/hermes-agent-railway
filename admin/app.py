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
# Z-API Webhook Handler (RaspadinhaShow) - BOT IA WHATSAPP
# FIX v2: filtro ReceivedCallback + modelo OpenRouter grátis
# ============================================================
import os
import httpx
import json
from datetime import datetime
from typing import Dict, List

ZAPI_INSTANCE_ID    = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_INSTANCE_TOKEN = os.getenv("ZAPI_INSTANCE_TOKEN")
ZAPI_CLIENT_TOKEN   = os.getenv("ZAPI_CLIENT_TOKEN")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")

ZAPI_BASE_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_INSTANCE_TOKEN}"
ZAPI_HEADERS  = {"Client-Token": ZAPI_CLIENT_TOKEN, "Content-Type": "application/json"}

# Memória de conversa em RAM (reseta no redeploy — ok para MVP)
conversation_memory: Dict[str, List[Dict]] = {}

# Deduplicação de messageId para evitar loop de retry do Z-API
processed_message_ids: set = set()
MAX_PROCESSED_IDS = 10000

SYSTEM_PROMPT = SYSTEM_PROMPT = """Você é o atendimento da RaspadinhaShow no WhatsApp. Fale como uma PESSOA da equipe — natural, simpática e informal. O dono é o Fábio.

# REGRAS ABSOLUTAS
1. Você só trata de assuntos da RaspadinhaShow (reposição, troca de kit, dúvidas do sorteio).
2. NUNCA diga que é IA, bot, assistente ou secretário virtual. Se perguntarem quem é, diga só "aqui é da RaspadinhaShow" — fale como gente da equipe.
3. Tom informal e humano: "Bom dia!", "Boa tarde!", "tudo bem?", chama pelo nome. Educado, sem apelidos íntimos.
4. Respostas curtas (1 a 3 frases), uma pergunta por vez, jeito de WhatsApp.
5. Nunca invente informação nem prometa prazo. Se não souber: "Vou confirmar aqui com a equipe e já te retorno, tá?"

# O NEGÓCIO
RaspadinhaShow = consignação de kits/grades (caça e pesca: facas, facões, amoladores, lanternas, canivetes; ou camisas de futebol), deixados em comércios locais. O estabelecimento não paga nada adiantado: vende e acerta depois.
- Sorteio: 825 fichas a R$ 1,00 cada, 10 números premiados. O cliente raspa e, se for premiado, leva o item na hora.
- Arremate: quem quiser leva todas as fichas restantes (quantidade x R$ 1,00) e ganha os 2 itens de arremate + todos os prêmios que ainda não saíram.
- Acerto: 25% do que foi vendido fica com o dono do comércio + 1 item de brinde. O kit fica ~30 a 45 dias no ponto.

# SUA TAREFA PRINCIPAL: registrar pedidos de reposição/troca
Quando o cliente disser que "a raspadinha acabou" ou "quer trocar a grade", colete de forma natural (não como formulário) APENAS 4 informações:
1. Nome do cliente
2. Nome do estabelecimento (bar, lanchonete, comércio)
3. Cidade
4. Telefone para contato
NÃO pergunte itens, sabores, quantidade nem dia/horário (a rota já é fixa).

Quando tiver as 4 informações, finalize com:
"Olá [Nome], anotei aqui: [Estabelecimento] em [Cidade]. Vou confirmar o dia certinho que passamos por aí e já te retorno, tá?"

# DÚVIDAS QUE VOCÊ RESPONDE
Mecânica do sorteio, arremate, comissão (25% + brinde) e prazo (30 a 45 dias). Explique de forma simples.

# FORA DO ESCOPO
Se o cliente puxar assunto que não é da RaspadinhaShow (política, futebol, cotações, vida pessoal, etc.), responda:
"Olha, eu só cuido da parte da RaspadinhaShow (reposição, troca de kit e dúvidas do sorteio). Sobre isso eu não consigo te ajudar, mas se precisar repor o kit é só falar!"
E não continue o assunto."""


async def send_zapi_message(phone: str, message: str) -> dict:
    """Envia mensagem via Z-API"""
    url = f"{ZAPI_BASE_URL}/send-text"
    payload = {"phone": phone, "message": message}
    print(f"[ZAPI Send] Enviando para {phone}: {message[:60]}...")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=ZAPI_HEADERS, json=payload)
    print(f"[ZAPI Send] Status: {r.status_code} | Response: {r.text[:200]}")
    return r.json()


def extract_message_text(data: dict) -> str:
    """
    Extrai texto da mensagem nos diferentes formatos do Z-API:
    - Formato antigo: {"message": {"text": "oi"}}
    - Formato novo:   {"text": {"message": "oi"}}
    - Formato simples:{"text": "oi"}
    """
    # Tenta "message.text" (formato antigo)
    msg_obj = data.get("message") or {}
    if isinstance(msg_obj, dict):
        t = msg_obj.get("text", "")
        if isinstance(t, str) and t.strip():
            return t.strip()
        if isinstance(t, dict):
            return (t.get("message") or t.get("text") or "").strip()

    # Tenta "text" no nível raiz (formato novo Z-API)
    raw_text = data.get("text", "")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    if isinstance(raw_text, dict):
        return (raw_text.get("message") or raw_text.get("text") or "").strip()

    return ""


def get_memory(phone: str) -> List[Dict]:
    if phone not in conversation_memory:
        conversation_memory[phone] = []
    return conversation_memory[phone]


def add_memory(phone: str, role: str, content: str):
    mem = get_memory(phone)
    mem.append({"role": role, "content": content})
    if len(mem) > 20:
        conversation_memory[phone] = mem[-20:]


async def call_llm(messages: List[Dict]) -> str:
    """Chama LLM via OpenRouter (modelo grátis)"""
    if not OPENROUTER_API_KEY:
        print("[Bot] OPENROUTER_API_KEY não configurada — usando fallback")
        return intelligent_fallback(messages)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json={
                    "model": "deepseek/deepseek-v4-flash",
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 300
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://raspadinhashow.com.br",
                    "X-Title": "RaspadinhaShow Bot"
                }
            )
            print(f"[Bot] LLM status: {r.status_code}")
            if r.status_code == 200:
                resposta = r.json()["choices"][0]["message"]["content"].strip()
                print(f"[Bot] LLM respondeu: {resposta[:80]}...")
                return resposta
            else:
                print(f"[Bot] LLM erro: {r.text[:200]}")
    except Exception as e:
        print(f"[Bot] LLM exceção: {e}")

    return intelligent_fallback(messages)


def intelligent_fallback(messages: List[Dict]) -> str:
    """Fallback sem LLM — fluxo baseado em palavras-chave"""
    user_msgs = [m["content"].lower() for m in messages if m.get("role") == "user"]
    last_msg  = user_msgs[-1] if user_msgs else ""
    all_text  = " ".join(user_msgs)

    produtos_lista = ["copinho", "palito", "saco", "xarope", "sabor", "copo", "colher", "tampa", "embalagem"]

    is_greeting    = any(p in last_msg for p in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "hello", "ei"])
    said_no        = any(p in last_msg for p in ["não preciso", "nao preciso", "não quero", "nao quero", "sem reposição", "nao vou", "não vou"])
    has_products   = any(p in all_text for p in produtos_lista)
    has_date       = any(p in last_msg for p in ["dia ", "amanhã", "hoje", "segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo", "manhã", "tarde", "noite"])
    confirmation   = any(p in last_msg for p in ["sim", "s", "ok", "pode", "certo", "beleza", "confirmo", "tá bom", "ta bom"])
    wants_products = any(p in last_msg for p in ["sim", "s", "quero", "preciso", "1"]) and not has_products

    if said_no:
        return "Sem problemas! Qualquer coisa é só chamar 🍓"

    if is_greeting and len(user_msgs) == 1:
        return "Olá! Tudo bem? 🍓 Sou a assistente da RaspadinhaShow. Você precisa de reposição de produtos?"

    if wants_products:
        return "Ótimo! Quais produtos você precisa? (copinhos, palitos, sacos, xaropes, sabores...)"

    if has_products and not has_date:
        prods = [p for p in produtos_lista if p in all_text]
        return f"Anotado: {', '.join(prods)} ✅\n\nQual o melhor dia e horário pra nossa equipe te visitar?"

    if has_products and has_date and not confirmation:
        return "Perfeito! Confirmo a visita no horário combinado com os produtos. Pode ser assim? ✅"

    if confirmation and has_products:
        return "Confirmado! ✅ Nossa equipe estará aí no horário combinado. Qualquer dúvida é só chamar 🍓"

    return "Não entendi completamente 😅 Me conta: quais produtos precisa e qual o melhor dia/horário pra visita?"


async def process_message(data: dict) -> dict:
    """Processa mensagem recebida e envia resposta"""
    phone        = data.get("phone") or data.get("from", "")
    message_text = extract_message_text(data)
    message_id   = data.get("messageId") or data.get("id", "")
    from_me      = data.get("fromMe", False)

    print(f"[Bot] phone={phone} | fromMe={from_me} | texto='{message_text}' | msgId={message_id}")

    # Ignora mensagens enviadas pelo próprio bot
    if from_me:
        return {"status": "ignored", "reason": "fromMe=true"}

    # Ignora se não tem telefone ou mensagem
    if not phone or not message_text:
        return {"status": "ignored", "reason": "sem phone ou texto"}

    # Deduplicação por messageId
    if message_id:
        if message_id in processed_message_ids:
            return {"status": "ignored", "reason": f"duplicado: {message_id}"}
        processed_message_ids.add(message_id)
        if len(processed_message_ids) > MAX_PROCESSED_IDS:
            to_remove = list(processed_message_ids)[:MAX_PROCESSED_IDS // 2]
            for mid in to_remove:
                processed_message_ids.discard(mid)

    # Adiciona mensagem do usuário à memória
    add_memory(phone, "user", message_text)

    # Monta contexto para o LLM
    memory = get_memory(phone)
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    llm_messages.extend({"role": m["role"], "content": m["content"]} for m in memory[-10:])

    # Chama LLM
    bot_response = await call_llm(llm_messages)
    add_memory(phone, "assistant", bot_response)

    # Envia resposta via Z-API
    if bot_response:
        await send_zapi_message(phone, bot_response)

    return {"status": "processed", "phone": phone, "preview": bot_response[:80]}


async def zapi_webhook(request: Request):
    """Endpoint do webhook Z-API"""
    try:
        data = await request.json()
        print(f"[ZAPI Webhook] RAW PAYLOAD: {json.dumps(data, ensure_ascii=False, indent=2)}")

        event_type = data.get("type") or data.get("event", "")

        # FIX: inclui ReceivedCallback (formato real do Z-API)
        # e também mantém formatos alternativos para compatibilidade
        should_process = (
            event_type == "ReceivedCallback"
            or event_type in ["message", "receive", "text", "incoming"]
            or ("message" in data and event_type not in [
                "DeliveryCallback", "MessageStatusCallback",
                "PresenceChatCallback", "ConnectedCallback",
                "DisconnectedCallback", "AllChatsCallback"
            ])
        )

        if should_process:
            result = await process_message(data)
            return JSONResponse({"received": True, "result": result})

        # Outros eventos (delivery, status, presence) — só confirma recebimento
        return JSONResponse({"received": True, "event": event_type})

    except Exception as e:
        print(f"[ZAPI Webhook] Erro: {e}")
        return JSONResponse({"received": False, "error": str(e)}, status_code=500)


async def zapi_webhook_health(request: Request):
    """Health check do webhook"""
    return JSONResponse({
        "status": "ok",
        "service": "raspadinhashow-whatsapp-bot",
        "instance": ZAPI_INSTANCE_ID,
        "llm": "openrouter" if OPENROUTER_API_KEY else "fallback"
    })


async def _is_authenticated(request: Request) -> bool:
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

# ============================================================
# Meta WhatsApp Cloud API Webhook (RaspadinhaShow) — OFICIAL
# Reusa memória + LLM + persona; recebe e envia pela Meta.
# ============================================================
from starlette.responses import PlainTextResponse

WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_GRAPH_VERSION   = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")


async def send_meta_message(phone: str, message: str) -> dict:
    """Envia mensagem pela Meta WhatsApp Cloud API (oficial)."""
    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": message}}
    print(f"[Meta Send] -> {phone}: {message[:60]}...")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    print(f"[Meta Send] Status: {r.status_code} | {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text}


async def process_message_meta(phone: str, message_text: str, message_id: str) -> dict:
    """Mesma lógica do bot (memória + LLM + persona), respondendo pela Meta."""
    if not phone or not message_text:
        return {"status": "ignored", "reason": "sem phone ou texto"}
    if message_id:
        if message_id in processed_message_ids:
            return {"status": "ignored", "reason": f"duplicado: {message_id}"}
        processed_message_ids.add(message_id)
        if len(processed_message_ids) > MAX_PROCESSED_IDS:
            for mid in list(processed_message_ids)[:MAX_PROCESSED_IDS // 2]:
                processed_message_ids.discard(mid)
    add_memory(phone, "user", message_text)
    memory = get_memory(phone)
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    llm_messages.extend({"role": m["role"], "content": m["content"]} for m in memory[-10:])
    bot_response = await call_llm(llm_messages)
    add_memory(phone, "assistant", bot_response)
    if bot_response:
        await send_meta_message(phone, bot_response)
    return {"status": "processed", "phone": phone, "preview": bot_response[:80]}


async def meta_webhook(request: Request):
    """GET = handshake de verificação da Meta. POST = mensagens recebidas."""
    if request.method == "GET":
        p = request.query_params
        if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
            print("[Meta Webhook] Verificacao OK")
            return PlainTextResponse(p.get("hub.challenge", ""))
        print("[Meta Webhook] Verificacao FALHOU")
        return PlainTextResponse("forbidden", status_code=403)
    try:
        data = await request.json()
        print(f"[Meta Webhook] RAW: {json.dumps(data, ensure_ascii=False)[:1000]}")
        results = []
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue  # por enquanto só texto
                    phone = msg.get("from", "")
                    message_text = (msg.get("text") or {}).get("body", "").strip()
                    message_id = msg.get("id", "")
                    results.append(await process_message_meta(phone, message_text, message_id))
        return JSONResponse({"received": True, "results": results})
    except Exception as e:
        print(f"[Meta Webhook] Erro: {e}")
        return JSONResponse({"received": True, "error": str(e)})  # 200 sempre, evita reenvio em loop
routes = [
    Route("/tui", tui_page, methods=["GET"]),
    WebSocketRoute("/tui/ws/auth/{provider}", hermes_terminal.login_ws),
    WebSocketRoute("/tui/ws/shell", hermes_terminal.shell_ws),
    Mount(DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app()),
    # Z-API Webhook endpoints (RaspadinhaShow)
    Route("/webhook/zapi", zapi_webhook, methods=["POST"]),
    Route("/webhook/zapi/health", zapi_webhook_health, methods=["GET"]),
      # Meta WhatsApp Cloud API (oficial)
    Route("/webhook/meta", meta_webhook, methods=["GET", "POST"]),
    # Catch-all proxy
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]

app = Starlette(debug=False, routes=routes)
# redeploy: forçar deploy do modelo deepseek
# redeploy 2 (auto-deploy religado)
