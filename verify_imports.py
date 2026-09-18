"""Quick verification script to test that core Kelpie imports work correctly."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ollama"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Logger"))

print("Verifying ollama_client imports...")

try:
    from ollama_client import load_config, get_client, resolve_model, send_message
    print("  [OK] ollama_client (load_config, get_client, resolve_model, send_message)")
except ImportError as e:
    print(f"  [FAIL] ollama_client: {e}")

print("\nVerifying config loads...")
try:
    config = load_config()
    assert config.get("default_model"), "default_model should be set"
    print("  [OK] Config loaded and default_model verified")
except Exception as e:
    print(f"  [FAIL] Config verification: {e}")

print("Done.")
