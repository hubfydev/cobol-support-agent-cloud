
# COBOL Support Agent — Custo Zero (Ollama) — v5 SAFE MOVE
- Movimentação IMAP segura: só apaga após copiar OK.
- Autoajuste de mailbox: tenta `INBOX/<pasta>` conforme delimitador do servidor.
- `EXPUNGE_AFTER_COPY=false` por padrão (seguro para testes).
- Logs configuráveis por `LOG_LEVEL`.

## Passos
1. Instalar Ollama: https://ollama.com/download
2. Baixar modelo: `ollama pull llama3.1:8b` ou `ollama pull phi3:3.8b`
3. Copiar `.env.example` para `.env` e **ATUALIZAR `MAIL_PASS`** com sua senha.
4. Criar venv e instalar deps:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```
5. Rodar:
   ```bash
   python app.py
   ```
6. Testar com e-mail fictício. Se a cópia funcionar, pode definir `EXPUNGE_AFTER_COPY=true`.
