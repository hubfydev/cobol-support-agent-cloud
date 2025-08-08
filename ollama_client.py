
import json, requests

class OllamaClient:
    def __init__(self, host:str, model:str):
        self.host = host.rstrip('/')
        self.model = model

    def generate_json(self, system_prompt:str, user_prompt:str, timeout=180):
        full_prompt = f"[SYSTEM]\n{system_prompt}\n[/SYSTEM]\n[USER]\n{user_prompt}\n[/USER]\n\nResponda SOMENTE com um JSON válido conforme o esquema pedido."
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "options": {"temperature": 0.2}
        }
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        text = data.get("response","").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end+1]
        return json.loads(text)
