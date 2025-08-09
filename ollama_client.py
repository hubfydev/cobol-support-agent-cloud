import json
import requests
import re


class OllamaClient:
    def __init__(self, host: str, model: str):
        self.host = host.rstrip('/')
        self.model = model

    def _strip_code_fences(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            # remove ```json ... ```
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()

    def _sanitize_controls(self, text: str) -> str:
        # remove caracteres de controle proibidos no JSON (0x00-0x1F, exceto \n e \t se estiverem fora de strings)
        # como heurística simples, substitui por espaço
        return re.sub(r'[\x00-\x1f\x7f]', ' ', text)

    def generate_json(self, system_prompt: str, user_prompt: str, timeout=180):
        # Instruções mais rígidas
        full_prompt = (
            "[SYSTEM]\n"
            + system_prompt
            + "\nRegras adicionais: responda SOMENTE um objeto JSON válido; "
              "não use crases, não use markdown, escape quebras de linha como \\n dentro de strings.\n"
            "[/SYSTEM]\n[USER]\n"
            + user_prompt
            + "\n[/USER]"
        )

        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            # *** ESTE É O PULO DO GATO: força JSON estruturado quando suportado ***
            "format": "json",
            "options": {
                "temperature": 0.2
            }
        }
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        text = data.get("response", "").strip()

        # Limpezas defensivas
        text = self._strip_code_fences(text)
        try:
            return json.loads(text)
        except Exception:
            text2 = self._sanitize_controls(text)
            try:
                return json.loads(text2)
            except Exception as e:
                # fallback: devolve objeto mínimo para escalar
                return {
                    "assunto": "Re: dúvida de COBOL",
                    "corpo_markdown": "(Não consegui entender o conteúdo automaticamente. Pode reenviar o código ou colar o erro completo?)",
                    "nivel_confianca": 0.0,
                    "acao": "escalar",
                    "_debug": f"json_error: {e} | raw: {text[:300]}"
                }
