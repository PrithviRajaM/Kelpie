"""
Web Search Agent - llm-axe powered web search, RAG, and markdown conversion.

Provides three core capabilities for Kelpie:
  1. Web Search - Uses llm-axe OnlineAgent to search the internet and return
     structured answers grounded in real web content.
  2. Website Content Extraction - Uses llm-axe WebsiteReaderAgent to fetch a
     specific URL and convert its content to clean markdown.
  3. RAG (Retrieval-Augmented Generation) - Fetches web content, splits it into
     chunks, finds the most relevant passages via embeddings, and provides them
     as context to the LLM for grounded responses.

Requires:
  - llm-axe (pip install llm-axe)
  - ollama Python package (pip install ollama)
  - beautifulsoup4, requests (transitive via llm-axe)
"""

import os
import sys
import re
import warnings

# Resolve paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Logger"))

import kelpie_logger as logger

try:
    from llm_axe import OllamaChat, OnlineAgent, WebsiteReaderAgent
    from llm_axe.core import (
        internet_search,
        read_website,
        split_into_chunks,
        find_most_relevant,
    )
except ImportError as e:
    print(
        "The 'llm-axe' package is required. Install it with: pip install llm-axe\n"
        f"Import error: {e}"
    )
    sys.exit(1)

try:
    import ollama as ollama_lib
except ImportError:
    print("The 'ollama' package is required. Install it with: pip install ollama")
    sys.exit(1)


SCRIPT_NAME = "web_search_agent.py"


# ---------------------------------------------------------------------------
# Markdown Conversion Utilities
# ---------------------------------------------------------------------------

def html_to_markdown(html_text: str) -> str:
    """Convert raw website text content into clean markdown format.

    Applies basic formatting rules to structure the extracted text:
    - Preserves paragraph breaks
    - Converts obvious headings (short standalone lines) to markdown headers
    - Cleans up excessive whitespace

    Args:
        html_text: Raw text extracted from a webpage (already stripped of HTML tags).

    Returns:
        Cleaned markdown-formatted string.
    """
    if not html_text:
        return ""

    lines = html_text.split("\n")
    markdown_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            markdown_lines.append("")
            continue

        # Heuristic: short lines (< 80 chars) that don't end with punctuation
        # are likely headings or section titles
        if len(stripped) < 80 and not stripped.endswith((".", ",", ";", ":", "!", "?")):
            # Check if it looks like a heading (mostly alpha, no trailing punct)
            if re.match(r"^[A-Z]", stripped) and not re.search(r"[.!?]$", stripped):
                markdown_lines.append(f"## {stripped}")
                continue

        markdown_lines.append(stripped)

    # Clean up multiple consecutive blank lines
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(markdown_lines))
    return result.strip()


def website_to_markdown(url: str) -> str | None:
    """Fetch a website and return its content as structured markdown.

    Uses llm-axe's read_website to extract the page body, then applies
    markdown conversion for clean, structured output.

    Args:
        url: The URL to fetch and convert.

    Returns:
        Markdown-formatted content string, or None on failure.
    """
    logger.log_info(SCRIPT_NAME, f"Fetching website content for markdown conversion: {url}")

    try:
        raw_content = read_website(url)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Failed to fetch website '{url}': {e}")
        return None

    if not raw_content:
        logger.log_warning(SCRIPT_NAME, f"No content retrieved from '{url}'.")
        return None

    markdown_content = html_to_markdown(raw_content)
    logger.log_info(
        SCRIPT_NAME,
        f"Converted website content to markdown ({len(markdown_content)} chars) from '{url}'.",
    )
    return markdown_content


# ---------------------------------------------------------------------------
# Web Search via llm-axe OnlineAgent
# ---------------------------------------------------------------------------

def perform_web_search(query: str, ollama_host: str, model: str) -> str | None:
    """Perform an internet search using llm-axe's OnlineAgent.

    The OnlineAgent will:
    1. Generate an optimized search query from the user prompt.
    2. Search the internet (via Google search).
    3. Pick the most relevant URL from results.
    4. Read the chosen website content.
    5. Use the LLM to synthesize a response grounded in web data.

    Args:
        query: The user's question or search prompt.
        ollama_host: The Ollama server URL (e.g. http://localhost:11434).
        model: The Ollama model name to use.

    Returns:
        The web-grounded response string, or None on failure.
    """
    logger.log_info(SCRIPT_NAME, f"Performing web search for: '{query}' using model '{model}'")

    try:
        llm = OllamaChat(host=ollama_host, model=model)
        searcher = OnlineAgent(llm)
        response = searcher.search(query)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Web search failed: {e}")
        return None

    if response:
        logger.log_info(SCRIPT_NAME, f"Web search completed ({len(response)} chars).")
    else:
        logger.log_warning(SCRIPT_NAME, "Web search returned no results.")

    return response


# ---------------------------------------------------------------------------
# Website Content Reader via llm-axe WebsiteReaderAgent
# ---------------------------------------------------------------------------

def read_website_with_llm(url: str, question: str, ollama_host: str, model: str) -> str | None:
    """Read a specific website and answer a question based on its content.

    Uses llm-axe's WebsiteReaderAgent which fetches the page, provides the
    content to the LLM, and generates a focused answer.

    Args:
        url: The website URL to read.
        question: The question to answer based on the website content.
        ollama_host: The Ollama server URL.
        model: The Ollama model name.

    Returns:
        The LLM's answer grounded in the website content, or None on failure.
    """
    logger.log_info(SCRIPT_NAME, f"Reading website '{url}' to answer: '{question}'")

    try:
        llm = OllamaChat(host=ollama_host, model=model)
        reader = WebsiteReaderAgent(llm)
        response = reader.ask(question=question, url=url)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"Website reading failed for '{url}': {e}")
        return None

    if response:
        logger.log_info(SCRIPT_NAME, f"Website reading completed ({len(response)} chars).")
    else:
        logger.log_warning(SCRIPT_NAME, f"No answer generated from website '{url}'.")

    return response


# ---------------------------------------------------------------------------
# RAG (Retrieval-Augmented Generation)
# ---------------------------------------------------------------------------

def fetch_and_rag(url: str, question: str, ollama_host: str, model: str,
                  embedding_model: str = "nomic-embed-text",
                  top_k: int = 5, sentences_per_chunk: int = 3) -> str | None:
    """Fetch web content, chunk it, find relevant passages via embeddings, and
    generate a grounded answer using RAG.

    Process:
    1. Fetch the webpage content (raw text).
    2. Split content into sentence-based chunks.
    3. Generate embeddings for each chunk using the embedding model.
    4. Generate an embedding for the user's question.
    5. Find the top-k most relevant chunks via cosine similarity.
    6. Pass the relevant chunks + question to the LLM for a final answer.

    Args:
        url: The webpage URL to use as the knowledge source.
        question: The user's question to answer.
        ollama_host: The Ollama server URL.
        model: The Ollama model name for generation.
        embedding_model: The Ollama model for embeddings (default: nomic-embed-text).
        top_k: Number of most relevant chunks to retrieve (default: 5).
        sentences_per_chunk: Sentences per chunk for splitting (default: 3).

    Returns:
        The RAG-grounded response, or None on failure.
    """
    logger.log_info(SCRIPT_NAME, f"Starting RAG pipeline for '{url}' with question: '{question}'")

    # Step 1: Fetch website content
    try:
        raw_content = read_website(url)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"RAG: Failed to fetch website '{url}': {e}")
        return None

    if not raw_content:
        logger.log_warning(SCRIPT_NAME, f"RAG: No content retrieved from '{url}'.")
        return None

    logger.log_info(SCRIPT_NAME, f"RAG: Fetched {len(raw_content)} chars from '{url}'.")

    # Step 2: Split into chunks
    chunks = split_into_chunks(raw_content, sentences_per_chunk)
    if not chunks:
        logger.log_warning(SCRIPT_NAME, "RAG: Content could not be split into chunks.")
        return None

    logger.log_info(SCRIPT_NAME, f"RAG: Split content into {len(chunks)} chunks.")

    # Step 3: Generate embeddings for each chunk
    ollama_client = ollama_lib.Client(host=ollama_host)
    text_embedding_pairs = []

    try:
        for chunk in chunks:
            if not chunk.strip():
                continue
            embedding_response = ollama_client.embeddings(
                model=embedding_model, prompt=chunk
            )
            embedding = embedding_response["embedding"]
            text_embedding_pairs.append((chunk, embedding))
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"RAG: Embedding generation failed: {e}")
        return None

    if not text_embedding_pairs:
        logger.log_warning(SCRIPT_NAME, "RAG: No embeddings generated.")
        return None

    logger.log_info(SCRIPT_NAME, f"RAG: Generated {len(text_embedding_pairs)} embeddings.")

    # Step 4: Embed the question
    try:
        question_embedding_response = ollama_client.embeddings(
            model=embedding_model, prompt=question
        )
        question_embedding = question_embedding_response["embedding"]
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"RAG: Question embedding failed: {e}")
        return None

    # Step 5: Find most relevant chunks
    relevant_chunks = find_most_relevant(
        text_embedding_pairs, question_embedding, top_k=min(top_k, len(text_embedding_pairs))
    )
    logger.log_info(SCRIPT_NAME, f"RAG: Found {len(relevant_chunks)} relevant chunks.")

    # Step 6: Build context and generate answer
    context = "\n\n".join(relevant_chunks)
    rag_prompt = (
        f"Based on the following information retrieved from {url}:\n\n"
        f"---\n{context}\n---\n\n"
        f"Answer the following question accurately and concisely:\n{question}"
    )

    try:
        llm = OllamaChat(host=ollama_host, model=model)
        from llm_axe import Agent
        responder = Agent(llm, custom_system_prompt="You are a helpful assistant. Answer based only on the provided context.")
        response = responder.ask(rag_prompt)
    except Exception as e:
        logger.log_error(SCRIPT_NAME, f"RAG: Final generation failed: {e}")
        return None

    if response:
        logger.log_info(SCRIPT_NAME, f"RAG: Generated response ({len(response)} chars).")
    else:
        logger.log_warning(SCRIPT_NAME, "RAG: No response generated.")

    return response


# ---------------------------------------------------------------------------
# Unified Web-Enriched Response (used by ollama_client.send_message_with_web)
# ---------------------------------------------------------------------------

def get_web_enriched_context(message: str, ollama_host: str, model: str,
                             embedding_model: str = "nomic-embed-text") -> str | None:
    """Analyze a message and return web-enriched context.

    This is the main entry point used by ollama_client.send_message_with_web.
    It determines the best strategy:
    - If the message contains URLs → fetch those pages and convert to markdown.
    - If no URLs but web search is needed → perform a web search and return results.

    Args:
        message: The user's prompt/message.
        ollama_host: The Ollama server URL.
        model: The Ollama model name.
        embedding_model: Model for RAG embeddings.

    Returns:
        Structured web content (markdown format) to augment the prompt, or None.
    """
    # Extract URLs from the message
    url_pattern = r'https?://[^\s,\"\'>)}\]]+'
    urls = re.findall(url_pattern, message)

    if urls:
        # Strategy: Fetch each URL and convert to markdown
        logger.log_info(SCRIPT_NAME, f"Found {len(urls)} URL(s) in message. Fetching content...")
        all_content = []

        for url in urls:
            markdown = website_to_markdown(url)
            if markdown:
                all_content.append(
                    f"## Content from: {url}\n\n{markdown}\n"
                )

        if all_content:
            return "\n---\n\n".join(all_content)
        else:
            # Fallback: try RAG approach on the first URL
            logger.log_info(SCRIPT_NAME, "Direct fetch failed, trying RAG on first URL...")
            rag_result = fetch_and_rag(
                urls[0], message, ollama_host, model, embedding_model
            )
            return rag_result
    else:
        # Strategy: Perform web search using OnlineAgent
        logger.log_info(SCRIPT_NAME, "No URLs found. Performing web search...")
        search_result = perform_web_search(message, ollama_host, model)
        return search_result
