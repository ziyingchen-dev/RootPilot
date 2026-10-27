import os

from mistralai.client import Mistral


DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "magistral-medium-latest")


class MistralClient:
    def __init__(self, model=None, api_key=None):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY is not set.")
        try:
            self.client = Mistral(api_key=self.api_key)
        except Exception:
            raise RuntimeError("Failed to initialize Mistral client.") from None

    def _model_for_error(self):
        return self.model.replace(self.api_key, "[redacted]")

    def generate(self, prompt):
        try:
            response = self.client.chat.complete(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
        except Exception:
            raise RuntimeError(
                f"Failed to query Mistral model '{self._model_for_error()}'."
            ) from None

        choices = (
            response.get("choices")
            if isinstance(response, dict)
            else getattr(response, "choices", None)
        )
        if not isinstance(choices, (list, tuple)) or not choices:
            raise RuntimeError(
                f"Mistral model '{self._model_for_error()}' returned an "
                "unsupported response shape."
            )

        choice = choices[0]
        message = (
            choice.get("message")
            if isinstance(choice, dict)
            else getattr(choice, "message", None)
        )
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if not isinstance(content, str):
            raise RuntimeError(
                f"Mistral model '{self._model_for_error()}' returned an "
                "unsupported response shape."
            )
        if not content.strip():
            raise RuntimeError(
                f"Mistral model '{self._model_for_error()}' returned empty content."
            )
        return content
