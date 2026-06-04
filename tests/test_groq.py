"""Quick Groq API test — delete after verifying."""
import os
from dotenv import load_dotenv
load_dotenv(override=True)

key = os.environ.get("GROQ_API_KEY", "")
print(f"Key: {key[:8]}...{key[-4:]}" if len(key) > 12 else f"Key: {key!r}")

from groq import Groq
client = Groq(api_key=key)

models = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]

for m in models:
    try:
        r = client.chat.completions.create(
            model=m,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=5, temperature=0.0,
        )
        print(f"  [OK] {m}: {r.choices[0].message.content.strip()!r}")
    except Exception as e:
        print(f"  [ERROR] {m}: {str(e)[:120]}")
