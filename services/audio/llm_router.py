"""
Module: LLM Router & Fallback Manager
Purpose: Acts as a smart proxy for Large Language Models. Routes analysis requests
         to the primary provider (Groq/Llama-3) and automatically falls back to
         the secondary provider (Gemini) if the primary fails or times out.
"""