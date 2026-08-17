import os
from groq import Groq

api_key = os.getenv("GROQ_API_KEY")

print("API key exists:", bool(api_key))
print("API key prefix:", api_key[:10] + "..." if api_key else None)

client = Groq(api_key="")

print("\nAvailable models:")
models = client.models.list()

for model in models.data:
    print(model.id)