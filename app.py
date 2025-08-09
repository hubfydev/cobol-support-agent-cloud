import os
import ssl
import time
import json
import sqlite3
import smtplib
import email
import re
import base64
import imaplib
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from markdown import markdown
from dotenv import load_dotenv
from pathlib import Path
from prompts import SYSTEM_PROMPT, USER_TEMPLATE

# OAuth (Gmail)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ========= Carrega .env =========
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# -------- IMAP (leitura) no HostGator --------
IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
MAIL_USER = os.getenv("MAIL_USER")   # suporte@aprendacobol.com.br
MAIL_PASS = os.getenv("MAIL_PASS")   # senha do HostGator

# -------- Envio via Gmail OAuth --------
SMTP_MODE = os.getenv("SMTP_MODE", "gmail_oauth")  # deve ser gmail_oauth
# seu gmail (remetente técnico)
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GOOGLE_CLIENT_SECRET_FILE = os.getenv(
    "GOOGLE_CLIENT_SECRET_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
SMTP_DEBUG_ON = os.getenv("SMTP_DEBUG", "0") == "1"

# -------- LLM local --------
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# -------- Comportamento --------
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
FOLDER_PROCESSED = os.getenv("FOLDER_PROCESSED", "Respondidos")
FOLDER_ESCALATE = os.getenv("FOLDER_ESCALATE", "Escalar")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
EXPUNGE_AFTER_COPY = os.getenv("EXPUNGE_AFTER_COPY", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()
# "Sent" ou "Enviados" (Roundcube); código descobre INBOX.Sent
SENT_FOLDER = os.getenv("SENT_FOLDER", "Sent")

# -------- Assinatura --------
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Equipe Aprenda COBOL — Suporte")
SIGNATURE_FOOTER = os.getenv(
    "SIGNATURE_FOOTER", "Se precisar, responda este e-mail com mais detalhes ou anexe seu arquivo .COB/.CBL.\nHorário de atendimento: 9h–18h (ET), seg–sex.")
SIGNATURE_LINKS = os.getenv("SIGNATURE_LINKS", "")

if LLM_BACKEND == "ollama":
    from ollama_client import OllamaClient

DB_PATH = "state.db"

# ========= Utils =========


def log(level, *args):
    levels = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    if levels[level] >= levels.get(LOG_LEVEL, 1):
        print(f"[{level.upper()}]", *args)


def require_env():
    missing = []
    for k in ["IMAP_HOST", "MAIL_USER", "MAIL_PASS"]:
        if not globals().get(k):
            missing.append(k)
    if SMTP_MODE != "gmail_oauth":
        missing.append("SMTP_MODE deve ser gmail_oauth")
    if not GMAIL_EMAIL:
        missing.append("GMAIL_EMAIL")
    if not Path(GOOGLE_CLIENT_SECRET_FILE).exists():
        missing.append("credentials.json")
    if not Path(GOOGLE_TOKEN_FILE).exists():
        missing.append("token.json")
    if missing:
        raise SystemExit("Faltam variáveis/arquivos: " + ", ".join(missing))


def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY)")
    con.commit()
    con.close()


def already_processed(msgid: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM processed WHERE message_id=?", (msgid,))
    row = cur.fetchone()
    con.close()
    return row is not None


def mark_processed(msgid: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO processed(message_id) VALUES (?)", (msgid,))
    con.commit()
    con.close()

# ========= IMAP =========


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
            for part in m.iter_parts():
                walk(part)
        else:
            ctype = m.get_content_type()
            filename = m.get_filename()
            payload = m.get_payload(decode=True) or b""
            try:
                text = payload.decode(
                    m.get_content_charset() or "utf-8", errors="ignore")
            except:
                text = ""
            if filename and filename.lower().endswith((".cob", ".cbl", ".txt")):
                code_chunks.append(f"--- {filename} ---\n{text}")
            elif ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html" and not plain_parts:
                import re as _re
                plain_parts.append(_re.sub("<[^<]+?>", "", text))
    walk(msg)
    plain_text = "\n".join(plain_parts).strip()
    code_block = ""
    if code_chunks:
        code_block = "```\n" + "\n\n".join(code_chunks) + "\n```"
    elif "IDENTIFICATION DIVISION" in plain_text.upper():
        code_block = "```cobol\n" + plain_text + "\n```"
    return msg, msgid, from_addr, subject, plain_text, code_block


def guess_first_name(from_addr: str) -> str:
    local = from_addr.split("@")[0]
    local = re.sub(r"[._\-]+", " ", local).strip()
    parts = local.split()
    name = parts[0].capitalize() if parts else ""
    if name.lower() in {"contato", "aluno", "suporte", "noreply", "no"}:
        return ""
    return name

# ========= Helpers de LIST/parse =========


def _parse_list_line(line: str):
    """
    Parseia uma linha de LIST IMAP:
      (<flags>) "<delim>" <name>
    Retorna (flags, delim, name) ou (None,None,None) se falhar.
    """
    m = re.search(
        r'\((?P<flags>.*?)\)\s+"(?P<delim>[^"]+)"\s+(?P<name>.*)$', line.strip())
    if not m:
        return None, None, None
    flags = m.group("flags").strip()
    delim = m.group("delim")
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return flags, delim, name


_listed_boxes_printed = False  # flag global simples


def _list_mailboxes_once(imap):
    global _listed_boxes_printed
    boxes = {}
    if _listed_boxes_printed:
        return boxes
    try:
        typ, data = imap.list()
        if typ == "OK":
            print("[DEBUG] LIST mailboxes:")
            for raw in (data or []):
                line = raw.decode(errors="ignore")
                print("   ", line)
                flags, delim, name = _parse_list_line(line)
                if name:
                    boxes[name] = {"flags": flags, "delim": delim}
        else:
            print("[WARN] LIST não retornou OK:", data)
    except Exception as e:
        print("[WARN] Falha ao listar mailboxes:", e)
    _listed_boxes_printed = True
    return boxes

# ========= Mover robusto (COPY e UID COPY) =========


def move_message(imap, num, dest_folder):
    """
    Move a mensagem para dest_folder:
    1) tenta COPY com sequence number
    2) se falhar, tenta UID COPY (buscando UID primeiro)
    3) marca como \Deleted e deixa o EXPUNGE pro final do ciclo
    """
    existing = _list_mailboxes_once(imap)  # dict: name -> {flags,delim}
    # monte candidatos
    candidates = [dest_folder]
    for sep in ("/", "."):
        if not dest_folder.upper().startswith("INBOX"):
            candidates.append(f"INBOX{sep}{dest_folder}")

    # prioriza nomes que existem *exatamente* no servidor
    existing_names = list(existing.keys())
    exact = [n for n in existing_names if n.lower() == dest_folder.lower()]
    ends = [n for n in existing_names if n.lower().endswith(dest_folder.lower())]
    ordered = []
    ordered += [n for n in exact if n not in ordered]
    ordered += [n for n in ends if n not in ordered]
    ordered += [c for c in candidates if c not in ordered]

    last_err = None

    # 1) COPY com sequence number
    for mb in ordered:
        try:
            imap.create(mb)
        except Exception:
            pass
        log("info", f"Tentando copiar para: {mb}")
        typ, resp = imap.copy(num, mb)
        log("debug", f"IMAP COPY -> typ={typ} resp={resp}")
        if typ == "OK":
            typ2, resp2 = imap.store(num, '+FLAGS', '\\Deleted')
            log("debug", f"IMAP STORE Deleted -> typ={typ2} resp={resp2}")
            if typ2 == "OK":
                return True
            last_err = (typ2, resp2)
        else:
            last_err = (typ, resp)

    # 2) UID COPY — pegar UID
    try:
        typ_uid, data_uid = imap.fetch(num, '(UID)')
        uid = None
        if typ_uid == "OK" and data_uid and data_uid[0]:
            m = re.search(rb'UID\s+(\d+)', data_uid[0])
            if m:
                uid = m.group(1).decode()
        if not uid:
            log("warn", "Não consegui obter UID para fallback UID COPY.")
        else:
            for mb in ordered:
                log("info", f"Tentando UID COPY para: {mb} (uid={uid})")
                typ, resp = imap.uid('COPY', uid, mb)
                log("debug", f"IMAP UID COPY -> typ={typ} resp={resp}")
                if typ == "OK":
                    typ2, resp2 = imap.uid(
                        'STORE', uid, '+FLAGS', '(\\Deleted)')
                    log("debug",
                        f"IMAP UID STORE Deleted -> typ={typ2} resp={resp2}")
                    if typ2 == "OK":
                        return True
                    last_err = (typ2, resp2)
                else:
                    last_err = (typ, resp)
    except Exception as e:
        log("warn", f"Falha no fallback UID COPY: {e}")

    log("warn", f"Falha ao mover para {dest_folder}. Último erro: {last_err}")
    return False


# ========= Gmail OAuth (XOAUTH2) =========
SCOPES = ["https://mail.google.com/"]


def get_gmail_credentials():
    creds = None
    if Path(GOOGLE_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError(
                "token.json ausente/inválido. Rode: python oauth_setup.py")
    return creds


def smtp_send_via_gmail_oauth(message: EmailMessage):
    creds = get_gmail_credentials()
    access_token = creds.token
    auth_string = base64.b64encode(
        f"user={GMAIL_EMAIL}\1auth=Bearer {access_token}\1\1".encode("utf-8")
    ).decode("utf-8")
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        if SMTP_DEBUG_ON:
            smtp.set_debuglevel(1)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        code, resp = smtp.docmd("AUTH", "XOAUTH2 " + auth_string)
        if code != 235:
            raise RuntimeError(f"XOAUTH2 falhou: {code} {resp}")
        smtp.send_message(message)

# ========= Append em Enviados (Roundcube) robusto =========


def append_to_sent(imap_host, imap_port, user, pwd, sent_folder_name, msg):
    try:
        ctx = ssl.create_default_context()
        im = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx)
        im.login(user, pwd)

        # lista mailboxes com parser robusto
        existing = {}
        try:
            typ, data = im.list()
            if typ == "OK":
                for raw in (data or []):
                    line = raw.decode(errors="ignore")
                    _, _, name = _parse_list_line(line)
                    if name:
                        existing[name] = True
        except Exception:
            pass

        # candidatos na ordem de preferência
        candidates = []
        if sent_folder_name:
            candidates.append(sent_folder_name)
        # comuns em cPanel/Roundcube (sua LIST mostrou INBOX.Sent como \Sent)
        for n in ("INBOX.Sent", "INBOX.Enviados", "Sent", "Enviados"):
            if n not in candidates:
                candidates.append(n)

        # reordena: existentes primeiro
        ordered = sorted(candidates, key=lambda x: (x not in existing, len(x)))
        dest = ordered[0]

        try:
            im.create(dest)
        except Exception:
            pass

        im.append(dest, "", imaplib.Time2Internaldate(
            time.time()), msg.as_bytes())
        im.logout()
        log("debug", f"Cópia enviada para pasta de enviados: {dest}")
    except Exception as e:
        log("warn", "Falha ao APPEND em Enviados:", e)

# ========= Assunto e assinatura =========


def make_reply_subject(original_subject: str) -> str:
    s = (original_subject or "").strip()
    # normaliza prefixos estranhos (re, Re , re : etc.)
    if s[:3].lower() == "re:":
        return "Re:" + s[3:]
    if s.lower().startswith("re :"):
        return "Re:" + s[4:]
    return f"Re: {s}" if s else "Re:"


def wrap_with_signature(first_name: str, body_markdown: str) -> str:
    saud = f"Olá{', ' + first_name if first_name else ''}!\n\n"
    sig_lines = ["\n---", f"**{SIGNATURE_NAME}**"]
    if SIGNATURE_FOOTER:
        sig_lines.append(SIGNATURE_FOOTER)
    if SIGNATURE_LINKS:
        sig_lines.append(SIGNATURE_LINKS)
    return saud + body_markdown.strip() + "\n" + "\n".join(sig_lines) + "\n"


def send_reply(original_msg, to_addr, reply_subject, body_markdown):
    body_html = markdown(body_markdown)

    reply = EmailMessage()
    reply["Subject"] = reply_subject

    # ===== Entregabilidade: From = Gmail, Reply-To = suporte@ =====
    reply["From"] = GMAIL_EMAIL
    reply["Reply-To"] = MAIL_USER
    # Se quiser enviar como suporte@ (menos recomendado até limpar reputação/DMARC),
    # troque para: reply["From"] = MAIL_USER  (e remova/ajuste Reply-To)

    reply["To"] = to_addr
    if original_msg.get("Message-ID"):
        reply["In-Reply-To"] = original_msg["Message-ID"]
        reply["References"] = original_msg["Message-ID"]

    reply.set_content(body_markdown)
    reply.add_alternative(body_html, subtype="html")

    log("info", f"Enviando resposta (Gmail OAuth) para {to_addr}…")
    smtp_send_via_gmail_oauth(reply)
    log("info", "Resposta enviada com sucesso (Gmail OAuth).")

    # (Opcional) guarda cópia no lado HostGator (Roundcube)
    try:
        append_to_sent(IMAP_HOST, IMAP_PORT, MAIL_USER,
                       MAIL_PASS, SENT_FOLDER, reply)
    except Exception as e:
        log("warn", "Não foi possível salvar cópia em Enviados:", e)

# ========= LLM / decisão =========


def call_agent_local(from_addr, subject, plain_text, code_block):
    user_prompt = USER_TEMPLATE.format(
        from_addr=from_addr, subject=subject,
        plain_text=plain_text[:8000], code_block=code_block[:8000]
    )
    if LLM_BACKEND == "ollama":
        try:
            client = OllamaClient(OLLAMA_HOST, OLLAMA_MODEL)
            data = client.generate_json(SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            log("warn", "Falha no Ollama:", e)
            data = {
                "assunto": f"Re: {subject[:200]}",
                "corpo_markdown": "(Tive um problema para interpretar sua mensagem automaticamente. Pode reenviar o código/anexo?)",
                "nivel_confianca": 0.0,
                "acao": "escalar"
            }
    else:
        body = ("- Entendi sua dúvida de COBOL e vou te ajudar com passos objetivos.\n"
                "- Verifique se suas DIVISION/SECTION estão declaradas corretamente.\n"
                "- Confira os níveis (01, 77) e as cláusulas PIC.\n"
                "- Para I/O, garanta OPEN/READ/WRITE/CLOSE.\n"
                "- Se puder, anexe seu .COB/.CBL para revisão mais precisa.\n")
        data = {"assunto": f"Re: {subject[:200]}", "corpo_markdown": body,
                "nivel_confianca": 0.4, "acao": "escalar"}
    return data

# ========= Loop principal =========


def main_loop():
    require_env()
    print("Watcher IMAP — envio via Gmail OAuth (XOAUTH2)")
    db_init()
    while True:
        try:
            imap = connect_imap()
            select_inbox(imap)
            ids = fetch_unseen(imap)
            log("debug", f"UNSEEN: {ids}")
            for num in ids:
                typ, data = imap.fetch(num, '(RFC822)')
                if typ != "OK":
                    continue
                raw = data[0][1]
                msg, msgid, from_addr, subject, plain_text, code_block = parse_message(
                    raw)
                if not msgid:
                    msgid = f"no-id-{num.decode()}-{int(time.time())}"
                if already_processed(msgid):
                    continue

                ai = call_agent_local(from_addr, subject,
                                      plain_text, code_block)
                action = ai.get("acao", "escalar")
                confidence = float(ai.get("nivel_confianca", 0.0))
                log("info", f"Ação={action} conf={confidence}")

                if action == "responder" and confidence >= CONFIDENCE_THRESHOLD:
                    first = guess_first_name(from_addr)
                    full_body = wrap_with_signature(
                        first, ai["corpo_markdown"])
                    reply_subject = make_reply_subject(subject)
                    log("info", f"Assunto final (reply): {reply_subject}")
                    send_reply(msg, from_addr, reply_subject, full_body)

                    log("info", f"Chamando move_message -> {FOLDER_PROCESSED}")
                    ok = move_message(imap, num, FOLDER_PROCESSED)
                    if not ok:
                        log("warn",
                            f"Não consegui mover para {FOLDER_PROCESSED}. Fallback: {FOLDER_ESCALATE}")
                        move_message(imap, num, FOLDER_ESCALATE)
                else:
                    log("info",
                        f"Chamando move_message -> {FOLDER_ESCALATE} (ação={action}, conf={confidence})")
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
