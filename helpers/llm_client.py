"""Android Control LLM Client - Async LLM calls via OpenAI-compatible APIs.

Supports: LMStudio, Ollama, Groq, OpenRouter, OpenAI.
Uses requests (sync) wrapped in asyncio.to_thread for non-blocking calls.
"""

import asyncio
import json
import logging

import requests

logger = logging.getLogger("droidclaw")

# Provider -> default API base URL mapping
_PROVIDER_URLS = {
    "lm_studio": "http://host.docker.internal:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "http://host.docker.internal:1234/v1",
    "llama_cpp": "http://host.docker.internal:8080/v1",
    "other": "http://localhost:8080/v1",
}


class LLMClient:
    """Async LLM client for OpenAI-compatible chat completions.

    Uses requests (sync) wrapped in asyncio.to_thread for non-blocking
    operation in async contexts.

    Args:
        provider: Provider name (lm_studio, ollama, groq, openrouter, openai).
        model: Model identifier string.
        api_base: Override API base URL. If empty, uses provider default.
        api_key: API key for providers that require authentication.
    """

    def __init__(self, provider: str, model: str, api_base: str = "", api_key: str = ""):
        self.provider = provider.lower().strip()
        self.model = model
        self.api_key = api_key

        if api_base:
            self.api_base = api_base.rstrip("/")
        else:
            self.api_base = _PROVIDER_URLS.get(
                self.provider, "http://localhost:11434/v1"
            )

        # Ensure api_base ends with /v1 for chat completions
        if not self.api_base.endswith("/v1"):
            self.api_base = self.api_base.rstrip("/") + "/v1"

        logger.info(
            "LLMClient initialized: provider=%s, model=%s, base=%s",
            self.provider,
            self.model,
            self.api_base,
        )

    @property
    def _chat_url(self) -> str:
        """Full URL for chat completions endpoint."""
        return f"{self.api_base}/chat/completions"

    def _build_headers(self) -> dict:
        """Build HTTP headers for the API request."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # OpenRouter requires additional headers
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://agent-zero.dev"
            headers["X-Title"] = "Agent Zero Android Control"
        return headers

    def _sync_chat(self, messages: list, temperature: float) -> str:
        """Synchronous chat call using requests.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            temperature: Sampling temperature.

        Returns:
            Assistant message content string.

        Raises:
            RuntimeError: On API errors or empty responses.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 1024,
        }

        headers = self._build_headers()
        url = self._chat_url

        logger.debug("LLM request to %s with %d messages", url, len(messages))

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=120,
            )

            if resp.status_code != 200:
                body = resp.text[:500]
                logger.error("LLM API error %d: %s", resp.status_code, body)
                raise RuntimeError(
                    f"LLM API returned status {resp.status_code}: {body[:200]}"
                )

            data = resp.json()

            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError("LLM API returned no choices")

            message = choices[0].get("message", {})
            content = message.get("content", "")

            if not content:
                raise RuntimeError("LLM API returned empty content")

            logger.debug("LLM response: %d chars", len(content))
            return content.strip()

        except requests.RequestException as exc:
            logger.error("LLM request failed: %s", exc)
            raise RuntimeError(f"LLM request failed: {exc}") from exc

    async def chat(self, messages: list, temperature: float = 0.3) -> str:
        """Send a chat completion request asynchronously.

        Wraps the synchronous requests call in asyncio.to_thread for
        non-blocking operation.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            temperature: Sampling temperature (0.0 - 2.0).

        Returns:
            The assistant message content as a string.
        """
        return await asyncio.to_thread(self._sync_chat, messages, temperature)

    async def chat_with_vision(
        self,
        messages: list,
        image_base64: str,
        temperature: float = 0.3,
    ) -> str:
        """Send a chat request with an image attachment for vision models.

        Injects the image into the last user message as OpenAI-format
        multimodal content.

        Args:
            messages: List of message dicts.
            image_base64: Base64-encoded image data.
            temperature: Sampling temperature.

        Returns:
            The assistant message content as a string.
        """
        enhanced_messages = list(messages)
        if enhanced_messages:
            last = enhanced_messages[-1]
            if last.get("role") == "user":
                enhanced_messages[-1] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": last["content"]},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_base64}",
                            },
                        },
                    ],
                }

        return await asyncio.to_thread(
            self._sync_chat, enhanced_messages, temperature
        )

    def __repr__(self) -> str:
        return (
            f"LLMClient(provider={self.provider}, model={self.model}, "
            f"base={self.api_base})"
        )
