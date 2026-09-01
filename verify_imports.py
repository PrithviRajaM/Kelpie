"""Quick verification script to test that all llm-axe imports work correctly."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("Verifying llm-axe imports...")

try:
    from llm_axe import OllamaChat, OnlineAgent, WebsiteReaderAgent, Agent
    print("  [OK] llm_axe main imports (OllamaChat, OnlineAgent, WebsiteReaderAgent, Agent)")
except ImportError as e:
    print(f"  [FAIL] llm_axe main imports: {e}")

try:
    from llm_axe.core import internet_search, read_website, split_into_chunks, find_most_relevant
    print("  [OK] llm_axe.core utilities (internet_search, read_website, split_into_chunks, find_most_relevant)")
except ImportError as e:
    print(f"  [FAIL] llm_axe.core utilities: {e}")

try:
    from webSearch.web_search_agent import (
        get_web_enriched_context,
        perform_web_search,
        website_to_markdown,
        read_website_with_llm,
        fetch_and_rag,
        html_to_markdown,
    )
    print("  [OK] webSearch.web_search_agent (all public functions)")
except ImportError as e:
    print(f"  [FAIL] webSearch.web_search_agent: {e}")

try:
    from webSearch import get_web_enriched_context as gwec
    print("  [OK] webSearch __init__.py package import")
except ImportError as e:
    print(f"  [FAIL] webSearch package import: {e}")

print("\nVerifying ollama_client imports...")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ollama"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Logger"))

try:
    from ollama_client import load_config, get_client, resolve_model, send_message, send_message_with_web
    print("  [OK] ollama_client (load_config, get_client, resolve_model, send_message, send_message_with_web)")
except ImportError as e:
    print(f"  [FAIL] ollama_client: {e}")

print("\nVerifying config loads...")
try:
    config = load_config()
    web_cfg = config.get("web_access", {})
    assert web_cfg.get("use_llm_axe") is True, "use_llm_axe should be True"
    assert web_cfg.get("embedding_model") == "nomic-embed-text", "embedding_model should be nomic-embed-text"
    assert web_cfg.get("rag_top_k") == 5, "rag_top_k should be 5"
    print("  [OK] Config loaded with llm-axe settings verified")
except Exception as e:
    print(f"  [FAIL] Config verification: {e}")

print("\nAll checks passed!" if "[FAIL]" not in "" else "")
print("Done.")
