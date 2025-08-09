from __future__ import print_function
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Escopo total do Gmail (SMTP/IMAP)
SCOPES = ['https://mail.google.com/']


def main():
    creds = None
    # Se já existir um token, tenta carregar
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    # Se não estiver válido, inicia login
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Salva o token para uso futuro
        with open('token.json', 'w') as token:
            token.write(creds.to_json())


if __name__ == '__main__':
    main()
