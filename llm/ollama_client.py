import os

import ollama

DEFAULT_MODEL = os.getenv("ROOTPILOT_MODEL", "qwen2.5:1.5b")
AVAILABLE_MODELS = [
    DEFAULT_MODEL,
    *(
        model
        for model in ("qwen2.5-coder:3b", "qwen2.5:1.5b")
        if model != DEFAULT_MODEL
    ),
]


class OllamaClient:
    def __init__(self, model=None, host=None):
        self.model = model or DEFAULT_MODEL
        self.host = host or os.getenv("OLLAMA_HOST")
        self.client = ollama.Client(host=self.host) if self.host else ollama.Client()

    def _model_for_error(self):
        api_key = os.getenv("MISTRAL_API_KEY")
        return self.model.replace(api_key, "[redacted]") if api_key else self.model

    def generate(self, prompt):
        try:
            response = self.client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                options={"temperature": 0.2},
            )
        except TypeError as exc:
            if "format" in str(exc) and "keyword" in str(exc):
                raise RuntimeError(
                    "The installed Ollama Python client does not support "
                    'structured JSON output via format="json". Upgrade the '
                    "ollama package to a compatible version."
                ) from None
            raise RuntimeError(
                f"Failed to query Ollama model '{self._model_for_error()}'."
            ) from None
        except Exception:
            raise RuntimeError(
                f"Failed to query Ollama model '{self._model_for_error()}'."
            ) from None

        if isinstance(response, dict):
            if "message" in response:
                message = response["message"]
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return self._validate_content(content)
            if "content" in response:
                content = response["content"]
                if isinstance(content, str):
                    return self._validate_content(content)

        if hasattr(response, "message"):
            message = getattr(response, "message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return self._validate_content(content)
            if hasattr(message, "content"):
                content = getattr(message, "content")
                if isinstance(content, str):
                    return self._validate_content(content)

        if hasattr(response, "content"):
            content = getattr(response, "content")
            if isinstance(content, str):
                return self._validate_content(content)

        if isinstance(response, str):
            return self._validate_content(response)

        raise RuntimeError(
            f"Ollama model '{self._model_for_error()}' returned an unsupported "
            "response shape."
        )

    def _validate_content(self, content):
        if not content.strip():
            raise RuntimeError(
                f"Ollama model '{self._model_for_error()}' returned empty content."
            )
        return content
