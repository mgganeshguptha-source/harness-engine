"""
list_models.py — print the models available to your Copilot login via the SDK.
Run:  python list_models.py
Spends no credits (just a metadata call).
"""
import asyncio
from copilot import CopilotClient


async def main():
    async with CopilotClient() as client:
        models = await client.list_models()
        print("Available models:\n")
        for m in models:
            # ModelInfo objects — print id + name defensively across shapes
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            name = getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else "")
            print(f"  {mid:<28} {name}")


if __name__ == "__main__":
    asyncio.run(main())
