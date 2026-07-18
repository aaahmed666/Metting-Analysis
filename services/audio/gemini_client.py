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
            # Prefer an explicit Gemini API key (get one at aistudio.google.com)
            self._client = genai.Client(api_key=api_key)
        elif settings.GOOGLE_APPLICATION_CREDENTIALS:
            # Fall back to a GCP service account credentials file via Vertex AI
            try:
                from google.oauth2 import service_account

                _SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
                creds = service_account.Credentials.from_service_account_file(
                    settings.GOOGLE_APPLICATION_CREDENTIALS,
                    scopes=_SCOPES,
                )
                self._client = genai.Client(
                    vertexai=True,
                    project=settings.GOOGLE_PROJECT_ID,
                    location=settings.VERTEX_AI_REGION,
                    credentials=creds
                )
                logger.debug(
                    "GeminiClient: using service account (Vertex AI)  file=%s",
                    settings.GOOGLE_APPLICATION_CREDENTIALS,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"GeminiClient: failed to load service account credentials "
                    f"from '{settings.GOOGLE_APPLICATION_CREDENTIALS}': {exc}"
                ) from exc
        else:
            raise RuntimeError(
                "GeminiClient: no credentials found. "
                "Set GEMINI_API_KEY in .env (https://aistudio.google.com/app/apikey) "
                "or set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON file."
            )

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
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
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

