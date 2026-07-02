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
import re
import html
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
WHATSAPP_GRAPH_VERSION   = os.getenv("WHATSAPP_GRAPH_VERSION", "v22.0")
WHATSAPP_REPOSICAO_TEMPLATE      = os.getenv("WHATSAPP_REPOSICAO_TEMPLATE", "disparo_reposicao")
WHATSAPP_REPOSICAO_TEMPLATE_LANG = os.getenv("WHATSAPP_REPOSICAO_TEMPLATE_LANG", "pt_BR")
RASPA_ADMIN_SECRET               = os.getenv("RASPA_ADMIN_SECRET", "")

# Groq (transcrição de áudio via Whisper)
GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

# Telegram (bot RaspadinhaShow — nomes próprios pra NÃO conflitar com o bot do Hermes)
TELEGRAM_BOT_TOKEN      = os.getenv("RASPA_TG_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("RASPA_TG_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("RASPA_TG_SECRET", "")

# Ponte de dados: cada interação é gravada aqui pro Hermes ler
LEADS_PATH  = "/data/raspadinha/leads.jsonl"
STATUS_PATH = "/data/raspadinha/status.jsonl"

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

# CONFIRMAÇÃO DE VISITA (cliente que já é atendido)
Às vezes o cliente já é da casa e só responde confirmando dia de visita (ex: "pode quinta", "passa quarta", "pode vir"). Nesse caso NÃO peça estabelecimento/cidade de novo. Apenas confirme de forma simpática: "Show! Anotei aqui pra passar [dia]. Qualquer coisa te aviso, tá?". Chame pelo nome só se ele já tiver dito o nome.

# DÚVIDAS QUE VOCÊ RESPONDE
Mecânica do sorteio, arremate, comissão (25% + brinde) e prazo (30 a 45 dias). Explique de forma simples.

# QUANDO CHAMAR O FÁBIO (escalonamento)
Se acontecer qualquer uma destas situações, comece sua resposta com a marca [ESCALAR] seguida de uma frase curta e natural pro cliente (ex: "[ESCALAR] Deixa eu confirmar uma coisa aqui com a equipe e já te retorno, tá?"):
- O cliente pedir pra falar com uma pessoa ou com o Fábio.
- Uma reclamação, problema ou algo que você não consegue resolver.
- Você não entender o que o cliente quer depois de tentar.
- Qualquer assunto sério ou fora do comum.
A marca [ESCALAR] é só um sinal interno no comecinho da mensagem — nunca explique ela pro cliente.

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


def registrar_status(destino: str, estado: str, raw: dict) -> None:
    """Salva o status de entrega (sent/delivered/read/failed) pro Hermes ler."""
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "destino": destino,
                "estado": estado,
                "errors": raw.get("errors", []),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Status] erro ao gravar: {e}")


def ja_processado(message_id: str) -> bool:
    """Dedup de messageId pra não responder duas vezes no retry da Meta."""
    if not message_id:
        return False
    if message_id in processed_message_ids:
        return True
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > MAX_PROCESSED_IDS:
        for mid in list(processed_message_ids)[:MAX_PROCESSED_IDS // 2]:
            processed_message_ids.discard(mid)
    return False


def get_memory(phone: str) -> List[Dict]:
    if phone not in conversation_memory:
        conversation_memory[phone] = []
    return conversation_memory[phone]


def add_memory(phone: str, role: str, content: str):
    mem = get_memory(phone)
    mem.append({"role": role, "content": content})
    if len(mem) > 20:
        conversation_memory[phone] = mem[-20:]


def historico_recente(phone: str, n: int = 3) -> str:
    """Monta um resumo das últimas N mensagens pra dar contexto nos alertas."""
    mem = conversation_memory.get(phone, [])
    if not mem:
        return ""
    linhas = []
    for m in mem[-n:]:
        quem = "Cliente" if m.get("role") == "user" else "Atendimento"
        txt = (m.get("content") or "").strip().replace("\n", " ")
        if len(txt) > 200:
            txt = txt[:200] + "…"
        linhas.append(f"{quem}: {txt}")
    return "\n".join(linhas)


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


# ----------------------------------------------------------------
# Mídia (áudio/imagem) e transcrição
# ----------------------------------------------------------------
async def baixar_midia_meta(media_id: str) -> bytes | None:
    """Baixa o binário de uma mídia (áudio, imagem) da Meta."""
    if not media_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            meta = await client.get(
                f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{media_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
            )
            if meta.status_code != 200:
                print(f"[Midia] erro metadados: {meta.status_code} {meta.text[:200]}")
                return None
            media_url = meta.json().get("url")
            if not media_url:
                return None
            bin_r = await client.get(media_url, headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"})
            if bin_r.status_code != 200:
                print(f"[Midia] erro download: {bin_r.status_code}")
                return None
            return bin_r.content
    except Exception as e:
        print(f"[Midia] exceção: {e}")
        return None


async def transcrever_audio(audio_bytes: bytes) -> str:
    """Transcreve áudio via Groq Whisper. Retorna '' se não der."""
    if not GROQ_API_KEY or not audio_bytes:
        return ""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": GROQ_WHISPER_MODEL, "language": "pt"},
            )
        if r.status_code == 200:
            txt = (r.json().get("text") or "").strip()
            print(f"[Groq] transcrição: {txt[:80]}")
            return txt
        print(f"[Groq] erro {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Groq] exceção: {e}")
    return ""


# ----------------------------------------------------------------
# Telegram (alertas + escalonamento)
# ----------------------------------------------------------------
async def enviar_telegram(texto: str) -> None:
    """Manda mensagem pro Fábio no bot central do Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram] não configurado — alerta perdido: {texto[:80]}")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
            )
        if r.status_code != 200:
            print(f"[Telegram] erro {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Telegram] exceção: {e}")


async def escalar(phone: str, name: str, motivo: str, conteudo: str) -> None:
    """Avisa o Fábio no Telegram que um cliente precisa dele."""
    quem = html.escape(name or phone)
    hist = historico_recente(phone)
    await enviar_telegram(
        f"🆘 <b>Atenção, Fábio</b>\n"
        f"👤 {quem}\n"
        f"💬 {html.escape(conteudo)}\n"
        f"⚠️ {html.escape(motivo)}\n"
        + (f"\n🗒️ <b>Contexto recente:</b>\n{html.escape(hist)}\n" if hist else "")
        + f"\n↩️ <i>Responda A ESTA mensagem pra falar direto com o cliente.</i>\n"
        f"#id:{phone}"
    )


async def enviar_telegram_foto(photo_bytes: bytes, legenda: str) -> None:
    """Encaminha a foto do cliente pro Telegram do Fábio."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not photo_bytes:
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": legenda, "parse_mode": "HTML"},
                files={"photo": ("foto.jpg", photo_bytes, "image/jpeg")},
            )
        if r.status_code != 200:
            print(f"[Telegram Foto] erro {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Telegram Foto] exceção: {e}")


# ----------------------------------------------------------------
# Envio Meta + processamento de texto
# ----------------------------------------------------------------
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


async def send_meta_template(phone: str, nome: str = "") -> dict:
    """Envia o template aprovado de reposicao fora da janela de 24h."""
    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    display_name = (nome or "tudo bem?").strip()
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": WHATSAPP_REPOSICAO_TEMPLATE,
            "language": {"code": WHATSAPP_REPOSICAO_TEMPLATE_LANG},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": display_name}],
                }
            ],
        },
    }
    print(f"[Meta Template] -> {phone}: {WHATSAPP_REPOSICAO_TEMPLATE} ({display_name})")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    print(f"[Meta Template] Status: {r.status_code} | {r.text[:300]}")
    try:
        data = r.json()
    except Exception:
        data = {"status_code": r.status_code, "text": r.text}
    registrar_status(phone, f"template_http_{r.status_code}", data if isinstance(data, dict) else {})
    return data


async def process_message_meta(phone: str, message_text: str, name: str = "") -> dict:
    """Memória + LLM + persona, responde pela Meta e grava o lead em /data."""
    if not phone or not message_text:
        return {"status": "ignored", "reason": "sem phone ou texto"}

    add_memory(phone, "user", message_text)
    memory = get_memory(phone)
    llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    llm_messages.extend({"role": m["role"], "content": m["content"]} for m in memory[-10:])
    bot_response = await call_llm(llm_messages)

    # Escalonamento: o LLM pediu pra chamar o Fábio
    escalar_flag = False
    if bot_response.startswith("[ESCALAR]"):
        escalar_flag = True
        bot_response = bot_response.replace("[ESCALAR]", "", 1).strip()
        if not bot_response:
            bot_response = "Deixa eu confirmar uma coisa aqui com a equipe e já te retorno, tá? 😊"

    add_memory(phone, "assistant", bot_response)
    if bot_response:
        await send_meta_message(phone, bot_response)
    if escalar_flag:
        await escalar(phone, name, "o bot achou melhor te chamar", f'Cliente disse: "{message_text}"')

    registrar_lead(phone, name, message_text, bot_response)
    return {"status": "processed", "phone": phone, "preview": bot_response[:80]}


async def meta_webhook(request: Request):
    """GET = handshake de verificação da Meta. POST = mensagens + status."""
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

                # 1) Status de entrega (sent / delivered / read / failed)
                for st in value.get("statuses", []):
                    estado = st.get("status", "")
                    destino = st.get("recipient_id", "")
                    print(f"[Meta Status] {estado} -> {destino} (id={st.get('id', '')})")
                    registrar_status(destino, estado, st)
                    if estado == "failed":
                        erros = st.get("errors", [])
                        detalhe = "; ".join(
                            f"{e.get('code')}: {e.get('title')} — {(e.get('error_data') or {}).get('details', '')}"
                            for e in erros
                        ) or "sem detalhe"
                        print(f"[Meta Status] FALHOU -> {destino}: {detalhe}")
                        await enviar_telegram(
                            f"❌ <b>Mensagem NÃO entregue</b>\n"
                            f"📱 {html.escape(destino)}\n"
                            f"⚠️ {html.escape(detalhe)}"
                        )

                # 2) Mensagens recebidas
                nomes = {c.get("wa_id", ""): (c.get("profile") or {}).get("name", "")
                         for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    phone = msg.get("from", "")
                    message_id = msg.get("id", "")
                    name = nomes.get(phone, "")
                    mtype = msg.get("type", "")

                    if ja_processado(message_id):
                        continue

                    if mtype == "text":
                        message_text = (msg.get("text") or {}).get("body", "").strip()
                        results.append(await process_message_meta(phone, message_text, name))

                    elif mtype == "audio":
                        media_id = (msg.get("audio") or {}).get("id", "")
                        audio_bytes = await baixar_midia_meta(media_id)
                        transcricao = await transcrever_audio(audio_bytes) if audio_bytes else ""
                        if transcricao:
                            results.append(await process_message_meta(phone, transcricao, name))
                        else:
                            add_memory(phone, "user", "[enviou um áudio]")
                            await send_meta_message(phone, "Opa, recebi seu áudio! Deixa eu ouvir aqui e já te respondo, tá? 😊")
                            await escalar(phone, name, "chegou um ÁUDIO que não consegui ouvir", "(áudio do cliente)")
                            results.append({"status": "escalated", "reason": "audio"})

                    elif mtype == "image":
                        await send_meta_message(phone, "Opa, recebi sua foto! Deixa eu dar uma olhada e já te falo, tá? 😊")
                        media_id = (msg.get("image") or {}).get("id", "")
                        legenda_cliente = (msg.get("image") or {}).get("caption", "")
                        add_memory(phone, "user", "[enviou uma foto]" + (f" com legenda: {legenda_cliente}" if legenda_cliente else ""))
                        img_bytes = await baixar_midia_meta(media_id)
                        quem = html.escape(name or phone)
                        hist = historico_recente(phone, 4)
                        legenda = (
                            f"🖼️ <b>Foto do cliente</b>\n"
                            f"👤 {quem}\n"
                            + (f"💬 {html.escape(legenda_cliente)}\n" if legenda_cliente else "")
                            + (f"\n🗒️ <b>Contexto recente:</b>\n{html.escape(hist)}\n" if hist else "")
                            + f"\n↩️ <i>Responda A ESTA foto pra falar com o cliente.</i>\n"
                            f"#id:{phone}"
                        )
                        if img_bytes:
                            await enviar_telegram_foto(img_bytes, legenda)
                        else:
                            await escalar(phone, name, "chegou uma FOTO que não consegui baixar", "(foto do cliente)")
                        results.append({"status": "escalated", "reason": "image"})

                    elif mtype in ("document", "video"):
                        add_memory(phone, "user", f"[enviou {mtype}]")
                        await send_meta_message(phone, "Opa, recebi aqui! Deixa eu dar uma olhada e já te falo, tá? 😊")
                        await escalar(phone, name, f"chegou {mtype.upper()} (precisa da sua olhada)", f"({mtype} do cliente)")
                        results.append({"status": "escalated", "reason": mtype})

                    elif mtype == "sticker":
                        continue  # figurinha = ignora (decisão do Fábio)

                    else:
                        continue

        return JSONResponse({"received": True, "results": results})
    except Exception as e:
        print(f"[Meta Webhook] Erro: {e}")
        return JSONResponse({"received": True, "error": str(e)})  # 200 sempre, evita reenvio em loop


async def telegram_webhook(request: Request):
    """Fábio responde no Telegram -> entrega no WhatsApp do cliente."""
    if TELEGRAM_WEBHOOK_SECRET:
        if request.headers.get("x-telegram-bot-api-secret-token") != TELEGRAM_WEBHOOK_SECRET:
            return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        texto = (msg.get("text") or "").strip()

        # só aceita do Fábio
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            return JSONResponse({"ok": True})
        if not texto:
            return JSONResponse({"ok": True})

        template_cmd = re.match(r"^/template\s+(\d{8,15})(?:\s+(.+))?$", texto, re.S)
        if template_cmd:
            phone = template_cmd.group(1)
            nome = (template_cmd.group(2) or "").strip()
            result = await send_meta_template(phone, nome)
            await enviar_telegram(
                f"🧪 <b>Template disparado</b>\n"
                f"📱 {html.escape(phone)}\n"
                f"📄 {html.escape(WHATSAPP_REPOSICAO_TEMPLATE)}\n"
                f"🔎 <code>{html.escape(json.dumps(result, ensure_ascii=False)[:500])}</code>"
            )
            return JSONResponse({"ok": True})

        # descobre o telefone do cliente
        phone = ""
        reply = msg.get("reply_to_message") or {}
        reply_text = reply.get("text") or reply.get("caption") or ""
        m = re.search(r"#id:(\d{8,15})", reply_text)
        if m:
            phone = m.group(1)
        else:
            cmd = re.match(r"^/r\s+(\d{8,15})\s+(.+)", texto, re.S)
            if cmd:
                phone = cmd.group(1)
                texto = cmd.group(2).strip()

        if not phone:
            await enviar_telegram(
                "⚠️ Não consegui identificar o cliente.\n"
                "Responda <b>a uma mensagem de alerta</b>, ou use:\n"
                "<code>/r 5511999999999 sua mensagem</code>"
            )
            return JSONResponse({"ok": True})

        await send_meta_message(phone, texto)
        add_memory(phone, "assistant", texto)
        registrar_lead(phone, "(via Fábio/Telegram)", "[resposta manual]", texto)
        await enviar_telegram(f"✅ Enviado pro cliente {html.escape(phone)}.")
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"[Telegram Webhook] erro: {e}")
        return JSONResponse({"ok": True})


async def meta_webhook_health(request: Request):
    """Health check do webhook Meta."""
    return JSONResponse({
        "status": "ok",
        "service": "raspadinhashow-meta",
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID,
        "llm": "openrouter" if OPENROUTER_API_KEY else "fallback",
        "audio": "groq" if GROQ_API_KEY else "off",
        "telegram": "on" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "off",
    })


async def disparo_template_test(request: Request):
    """Disparo controlado de template aprovado para teste/manual."""
    if not RASPA_ADMIN_SECRET:
        return JSONResponse({"ok": False, "error": "RASPA_ADMIN_SECRET nao configurado"}, status_code=403)
    if request.headers.get("x-raspa-admin-secret") != RASPA_ADMIN_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    phone = re.sub(r"\D+", "", str(data.get("phone", "")))
    nome = str(data.get("nome", "")).strip()
    if not phone:
        return JSONResponse({"ok": False, "error": "phone obrigatorio"}, status_code=400)
    result = await send_meta_template(phone, nome)
    await enviar_telegram(
        f"🧪 <b>Teste de template enviado</b>\n"
        f"📱 {html.escape(phone)}\n"
        f"📄 {html.escape(WHATSAPP_REPOSICAO_TEMPLATE)}"
    )
    return JSONResponse({"ok": True, "phone": phone, "template": WHATSAPP_REPOSICAO_TEMPLATE, "result": result})


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
    # Telegram (Fábio responde por lá)
    Route("/webhook/telegram", telegram_webhook, methods=["POST"]),
    # Disparo manual/teste de template (protegido por RASPA_ADMIN_SECRET)
    Route("/admin/raspadinha/template-test", disparo_template_test, methods=["POST"]),
    # Catch-all proxy para o resto (HTTP + WebSocket)
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]

app = Starlette(debug=False, routes=routes)
