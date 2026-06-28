"""Thin Starlette wrapper in front of hermes-webui.

Adds one new surface — `/tui` — that exposes an in-browser xterm with two modes:
  - OAuth one-shots: `hermes auth add <X> --type oauth --no-browser` for Codex /
    Nous Portal device-code flows (`/tui/ws/auth/<provider>`).
  - Free-form shell: `/bin/bash -i` for users without SSH access who need to
    run other `hermes` CLI commands or peek at `/data` (`/tui/ws/shell`).
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute

from . import proxy as hermes_proxy
from . import terminal as hermes_terminal
from .dashboard_proxy import DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app


TEMPLATE_PATH = Path(__file__).parent / "templates" / "tui.html"


# ============================================================
# RaspadinhaShow — Bot WhatsApp (Z-API legado + Meta oficial)
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

# Deduplicação de messageId para evitar loop de retry
processed_message_ids: set = set()
MAX_PROCESSED_IDS = 10000

# Meta WhatsApp Cloud API (oficial)
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_GRAPH_VERSION   = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

# Ponte de dados: cada interação é gravada aqui pro Hermes ler
LEADS_PATH = "/data/raspadinha/leads.jsonl"

SYSTEM_PROMPT = """Você é o atendimento da RaspadinhaShow no WhatsApp. Fale como uma PESSOA da equipe — natural, simpática e informal. O dono é o Fábio.

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


def registrar_lead(phone: str, name: str, text: str, reply: str) -> None:
    """Salva cada interação num arquivo no volume /data (o Hermes lê depois)."""
    try:
        os.makedirs(os.path.dirname(LEADS_PATH), exist_ok=True)
        with open(LEADS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "phone": phone,
                "name": name,
                "text": text,
                "reply": reply,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Lead] erro ao gravar: {e}")


def get_memory(phone: str) -> List[Dict]:
    if phone not in conversation_memory:
        conversation_memory[phone] = []
    return conversation_memory[phone]


def add_memory(phone: str, role: str, content: str):
    mem = get_memory(phone)
    mem.append({"role": role, "content": content})
    if len(mem) > 20:
        conversation_memory[phone] = mem[-20:]


def intelligent_fallback(messages: List[Dict]) -> str:
    """Fallback quando o LLM falha — resposta neutra e segura."""
    return ("Oi! Aqui é da RaspadinhaShow 😊 Como posso te ajudar com a "
            "reposição ou troca do kit?")


async def call_llm(messages: List[Dict]) -> str:
    """Chama LLM via OpenRouter."""
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


async def process_message_meta(phone: str, message_text: str, message_id: str, name: str = "") -> dict:
    """Memória + LLM + persona, responde pela Meta e grava o lead em /data."""
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
    registrar_lead(phone, name, message_text, bot_response)
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
                nomes = {c.get("wa_id", ""): (c.get("profile") or {}).get("name", "")
                         for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue  # por enquanto só texto
                    phone = msg.get("from", "")
                    message_text = (msg.get("text") or {}).get("body", "").strip()
                    message_id = msg.get("id", "")
                    name = nomes.get(phone, "")
                    results.append(await process_message_meta(phone, message_text, message_id, name))
        return JSONResponse({"received": True, "results": results})
    except Exception as e:
        print(f"[Meta Webhook] Erro: {e}")
        return JSONResponse({"received": True, "error": str(e)})  # 200 sempre, evita reenvio em loop


async def meta_webhook_health(request: Request):
    """Health check do webhook Meta."""
    return JSONResponse({
        "status": "ok",
        "service": "raspadinhashow-meta",
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID,
        "llm": "openrouter" if OPENROUTER_API_KEY else "fallback",
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


routes = [
    Route("/tui", tui_page, methods=["GET"]),
    WebSocketRoute("/tui/ws/auth/{provider}", hermes_terminal.login_ws),
    WebSocketRoute("/tui/ws/shell", hermes_terminal.shell_ws),
    Mount(DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app()),
    # Meta WhatsApp Cloud API (oficial)
    Route("/webhook/meta", meta_webhook, methods=["GET", "POST"]),
    Route("/webhook/meta/health", meta_webhook_health, methods=["GET"]),
    # Catch-all proxy para o resto (HTTP + WebSocket)
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]

app = Starlette(debug=False, routes=routes)
