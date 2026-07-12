"""
Module: LLM Client
Purpose: Manages communication with the Google Gemini API for all AI-powered
         insights generation in the pipeline (sentiment analysis, keyword
         detection, opening scripts, and recommended next actions).

Design notes
------------
* Uses the ``google-generativeai`` Python SDK.
* ``response_mime_type="application/json"`` constrains the model to return
  only valid JSON, so no post-processing stripping of markdown fences is needed.
* Raises ``LLMClientError`` on any API or JSON-decode failure so the caller
  (insights_generator) can handle errors cleanly without importing SDK internals.
* The client is intentionally stateless: use the module-level singleton via
  ``get_client()`` rather than constructing a new instance per request.
"""

from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import types

from config.setting import get_settings

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """Raised when the Gemini API call fails or returns unparseable JSON."""


class GeminiClient:
    """
    Thin wrapper around the Google Generative AI SDK for structured JSON
    completions.

    Usage::

        client = GeminiClient()
        result = client.complete(system=SYSTEM_PROMPT, user=USER_MESSAGE)
        # result is a parsed Python dict / list
    """

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.GEMINI_API_KEY.get_secret_value()
        
        if api_key:
            genai.configure(api_key=api_key)
        else:
            # Fall back to ADC (Application Default Credentials)
            import os
            if settings.GOOGLE_APPLICATION_CREDENTIALS:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS

        self._model_name: str = getattr(settings, "VERTEX_AI_MODEL", "gemini-2.5-flash")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> dict | list:
        """
        Send a generation request and return the parsed JSON response.

        Args:
            system:      System instruction that guides the model's behaviour.
            user:        User-turn content (e.g. the meeting transcript).
            temperature: Sampling temperature (default 0.2 for determinism).

        Returns:
            Parsed JSON object (``dict`` or ``list``).

        Raises:
            LLMClientError: On API errors or when the response is not valid JSON.
        """
        logger.debug("GeminiClient: sending request  model=%s", self._model_name)
        try:
            model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=system,
                generation_config=genai.GenerationConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:
            raise LLMClientError(f"Gemini API error: {exc}") from exc

        raw = (response.text or "").strip()
        logger.debug("GeminiClient: received response chars=%d", len(raw))

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMClientError(
                f"Gemini returned non-JSON content: {raw[:300]!r}"
            ) from exc

