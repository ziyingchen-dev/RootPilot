import os

import ollama

DEFAULT_MODEL = os.getenv("ROOTPILOT_MODEL", "qwen2.5-coder:3b")


class OllamaClient:
    def __init__(self, model=None, host=None):
        self.model = model or DEFAULT_MODEL
        self.host = host or os.getenv("OLLAMA_HOST")
        self.client = ollama.Client(host=self.host) if self.host else ollama.Client()

    def generate(self, prompt):
        try:
            response = self.client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.2},
            )
        except Exception as exc:  # pragma: no cover - exercised in real runtime only
            raise RuntimeError(f"Failed to query Ollama model '{self.model}': {exc}") from exc

        if isinstance(response, dict):
            if "message" in response:
                message = response["message"]
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
            if "content" in response:
                content = response["content"]
                if isinstance(content, str):
                    return content

        if hasattr(response, "message"):
            message = getattr(response, "message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
            if hasattr(message, "content"):
                content = getattr(message, "content")
                if isinstance(content, str):
                    return content

        if hasattr(response, "content"):
            content = getattr(response, "content")
            if isinstance(content, str):
                return content

        if isinstance(response, str):
            return response

        return str(response)
