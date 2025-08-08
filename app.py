import os, ssl, time, json, sqlite3, smtplib, email, re, sys
import imaplib
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from markdown import markdown
from dotenv import load_dotenv
from pathlib import Path
from prompts import SYSTEM_PROMPT, USER_TEMPLATE

# Carrega .env da mesma pasta do app.py
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# Variáveis principais
IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
MAIL_USER = os.getenv("MAIL_USER")
MAIL_PASS = os.getenv("MAIL_PASS")

LLM_BACKEND = os.getenv("LLM_BACKEND","ollama")
OLLAMA_HOST = os.getenv("OLLAMA_HOST","http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL","llama3.1:8b")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
FOLDER_PROCESSED = os.getenv("FOLDER_PROCESSED", "Respondidos")
FOLDER_ESCALATE = os.getenv("FOLDER_ESCALATE", "Escalar")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))

EXPUNGE_AFTER_COPY = os.getenv("EXPUNGE_AFTER_COPY","false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL","info").lower()

SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Equipe Aprenda COBOL — Suporte")
SIGNATURE_FOOTER = os.getenv("SIGNATURE_FOOTER", "Se precisar, responda este e-mail com mais detalhes ou anexe seu arquivo .COB/.CBL.\nHorário de atendimento: 9h–18h (ET), seg–sex.")
SIGNATURE_LINKS = os.getenv("SIGNATURE_LINKS", "")

if LLM_BACKEND == "ollama":
    from ollama_client import OllamaClient

DB_PATH = "state.db"

def log(level, *args):
    levels = {"debug":0,"info":1,"warn":2,"error":3}
    if levels[level] >= levels.get(LOG_LEVEL,1):
        print(f"[{level.upper()}]", *args)

def require_env():
    missing = [k for k in ["IMAP_HOST","SMTP_HOST","MAIL_USER","MAIL_PASS"] if not globals().get(k)]
    if missing:
        raise SystemExit("Faltam variáveis no .env (IMAP/SMTP/USER/PASS). Faltando: " + ", ".join(missing))

def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY)")
    con.commit(); con.close()

def already_processed(msgid:str)->bool:
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT 1 FROM processed WHERE message_id=?", (msgid,))
    row = cur.fetchone(); con.close()
    return row is not None

def mark_processed(msgid:str):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO processed(message_id) VALUES (?)", (msgid,))
    con.commit(); con.close()

def connect_imap():
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    imap.login(MAIL_USER, MAIL_PASS)
    return imap

def select_inbox(imap):
    typ, _ = imap.select("INBOX")
    if typ != "OK":
        raise RuntimeError("Não foi possível selecionar INBOX")

def fetch_unseen(imap):
    typ, data = imap.search(None, 'UNSEEN')
    if typ != "OK":
        return []
    return data[0].split()

def parse_message(raw_bytes):
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    msgid = msg.get("Message-ID") or msg.get("Message-Id") or ""
    from_addr = email.utils.parseaddr(msg.get("From"))[1]
    subject = msg.get("Subject", "")
    plain_parts, code_chunks = [], []

    def walk(m):
        if m.is_multipart():
            for part in m.iter_parts(): walk(part)
        else:
            ctype = m.get_content_type()
            filename = m.get_filename()
            payload = m.get_payload(decode=True) or b""
            try: text = payload.decode(m.get_content_charset() or "utf-8", errors="ignore")
            except: text = ""
            if filename and filename.lower().endswith((".cob",".cbl",".txt")):
                code_chunks.append(f"--- {filename} ---\n{text}")
            elif ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html" and not plain_parts:
                import re
                plain_parts.append(re.sub("<[^<]+?>", "", text))

    walk(msg)
    plain_text = "\n".join(plain_parts).strip()
    code_block = ""
    if code_chunks:
        code_block = "```\n" + "\n\n".join(code_chunks) + "\n```"
    elif "IDENTIFICATION DIVISION" in plain_text.upper():
        code_block = "```cobol\n" + plain_text + "\n```"
    return msg, msgid, from_addr, subject, plain_text, code_block

def guess_first_name(from_addr:str)->str:
    local = from_addr.split("@")[0]
    local = re.sub(r"[._\-]+", " ", local).strip()
    parts = local.split()
    name = parts[0].capitalize() if parts else ""
    if name.lower() in {"contato","aluno","suporte","noreply","no"}: return ""
    return name

def wrap_with_signature(first_name:str, body_markdown:str)->str:
    saud = f"Olá{', ' + first_name if first_name else ''}!\n\n"
    sig_lines = ["\n---", f"**{SIGNATURE_NAME}**"]
    if SIGNATURE_FOOTER: sig_lines.append(SIGNATURE_FOOTER)
    if SIGNATURE_LINKS: sig_lines.append(SIGNATURE_LINKS)
    return saud + body_markdown.strip() + "\n" + "\n".join(sig_lines) + "\n"

def ensure_folder(imap, mailbox_name: str) -> str:
    # Descobre delimitador e tenta criar caminho com/sem INBOX
    typ, data = imap.list()
    delim = "/"
    if typ == "OK" and data:
        sample = data[0].decode(errors="ignore")
        if '"' in sample:
            try:
                delim = sample.split('"')[1] or "/"
            except:
                pass
    candidates = [mailbox_name]
    if not mailbox_name.upper().startswith("INBOX"):
        candidates.append(f"INBOX{delim}{mailbox_name}")
    # tenta criar
    for mb in candidates:
        try: imap.create(mb)
        except: pass
    return candidates[-1]

def move_message(imap, num, dest_folder):
    dest = ensure_folder(imap, dest_folder)
    log("info", f"Movendo email para: {dest}")
    typ, resp = imap.copy(num, dest)
    if typ != "OK":
        log("warn", f"Falha ao copiar para '{dest}': {resp}")
        return  # não apaga se não copiou
    typ2, resp2 = imap.store(num, '+FLAGS', '\\Deleted')
    if typ2 != "OK":
        log("warn", f"Falha ao marcar como \\Deleted: {resp2}")

def call_agent_local(from_addr, subject, plain_text, code_block):
    user_prompt = USER_TEMPLATE.format(
        from_addr=from_addr, subject=subject,
        plain_text=plain_text[:8000], code_block=code_block[:8000]
    )
    if LLM_BACKEND == "ollama":
        client = OllamaClient(OLLAMA_HOST, OLLAMA_MODEL)
        data = client.generate_json(SYSTEM_PROMPT, user_prompt)
    else:
        body = ("- Entendi sua dúvida de COBOL e vou te ajudar com passos objetivos.\n"
                "- Verifique se suas DIVISION/SECTION estão declaradas corretamente.\n"
                "- Confira os níveis (01, 77) e as cláusulas PIC.\n"
                "- Para I/O, garanta OPEN/READ/WRITE/CLOSE.\n"
                "- Se puder, anexe seu .COB/.CBL para revisão mais precisa.\n")
        data = {"assunto": f"Re: {subject[:200]}", "corpo_markdown": body, "nivel_confianca": 0.4, "acao": "escalar"}
    return data

def send_reply(original_msg, to_addr, reply_subject, body_markdown):
    body_html = markdown(body_markdown)
    reply = EmailMessage()
    reply["Subject"] = reply_subject
    reply["From"] = MAIL_USER
    reply["To"] = to_addr
    if original_msg.get("Message-ID"):
        reply["In-Reply-To"] = original_msg["Message-ID"]
        reply["References"] = original_msg["Message-ID"]
    reply.set_content(body_markdown)
    reply.add_alternative(body_html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASS)
        smtp.send_message(reply)

def main_loop():
    require_env()
    # Log inicial
    print("Rodando watcher IMAP (modo custo zero) — v5 (safe move)")
    print(f"Backend: {LLM_BACKEND} | Modelo: {OLLAMA_MODEL if LLM_BACKEND=='ollama' else 'template'}")
    db_init()
    while True:
        try:
            imap = connect_imap()
            select_inbox(imap)
            ids = fetch_unseen(imap)
            log("debug", f"UNSEEN: {ids}")
            for num in ids:
                typ, data = imap.fetch(num, '(RFC822)')
                if typ != "OK": continue
                raw = data[0][1]
                msg, msgid, from_addr, subject, plain_text, code_block = parse_message(raw)
                if not msgid: msgid = f"no-id-{num.decode()}-{int(time.time())}"
                if already_processed(msgid):
                    log("debug", f"Já processado: {msgid}")
                    continue

                log("info", f"Processando: {subject} <{from_addr}>  id={msgid}")
                ai = call_agent_local(from_addr, subject, plain_text, code_block)
                action = ai.get("acao","escalar")
                confidence = float(ai.get("nivel_confianca",0.0))
                log("info", f"Ação={action} conf={confidence}")

                if action == "responder" and confidence >= CONFIDENCE_THRESHOLD:
                    first = guess_first_name(from_addr)
                    full_body = wrap_with_signature(first, ai["corpo_markdown"])
                    send_reply(msg, from_addr, ai["assunto"], full_body)
                    move_message(imap, num, FOLDER_PROCESSED)
                else:
                    move_message(imap, num, FOLDER_ESCALATE)

                mark_processed(msgid)

            if EXPUNGE_AFTER_COPY:
                log("debug", "Executando EXPUNGE…")
                imap.expunge()
            imap.logout()
        except Exception as e:
            log("error", "Erro no loop:", e)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main_loop()
