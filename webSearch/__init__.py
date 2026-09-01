"""
webSearch - llm-axe powered web search, RAG, and markdown conversion for Kelpie.

Public API:
  - get_web_enriched_context: Main entry point for ollama_client integration.
  - perform_web_search: Search the internet using OnlineAgent.
  - website_to_markdown: Fetch a URL and return markdown content.
  - read_website_with_llm: Read a website and answer a question about it.
  - fetch_and_rag: RAG pipeline (fetch → chunk → embed → retrieve → generate).
  - html_to_markdown: Convert raw text to structured markdown.
"""

from webSearch.web_search_agent import (
    get_web_enriched_context,
    perform_web_search,
    website_to_markdown,
    read_website_with_llm,
    fetch_and_rag,
    html_to_markdown,
)

__all__ = [
    "get_web_enriched_context",
    "perform_web_search",
    "website_to_markdown",
    "read_website_with_llm",
    "fetch_and_rag",
    "html_to_markdown",
]
