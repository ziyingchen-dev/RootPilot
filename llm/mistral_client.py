import os

from mistralai.client import Mistral


DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")


class MistralClient:
    def __init__(self, model=None, api_key=None):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY is not set.")
        self.client = Mistral(api_key=self.api_key)

    def generate(self, prompt):
        try:
            response = self.client.chat.complete(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to query Mistral model '{self.model}': {exc}"
            ) from exc

        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Mistral returned an empty response.")
        return content
