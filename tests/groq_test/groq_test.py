from groq import Groq

# Use your API key here
groq_client = Groq(api_key="")

print("=================================")
print("     WELCOME TO AI STUDY ASSISTANT")
print("=================================")
print('Type "quit", "exit" or "bye" to exit')
print()

while True:
    user_input_to_llm = input("USER: ")

    if user_input_to_llm.lower() in ["quit", "exit", "bye"]:
        print("\nStudy Assistant: Goodbye! Happy learning!")
        break

    print("\n" + "=" * 50)
    print("STUDY ASSISTANT:")
    print("=" * 50)

    stream = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": """
You are a helpful Study Assistant.

Always format your answers in a clear and organized way.

Formatting rules:
- Start with a clear title when appropriate.
- Use numbered lists for steps.
- Use bullet points for important points.
- Use short paragraphs instead of large blocks of text.
- Use labels such as "Answer:", "Example:", "Key Points:", and "Summary:" when useful.
- Use simple text formatting such as:
  [IMPORTANT]
  [EXAMPLE]
  [TIP]
- For code, put the code inside clear code blocks.
- For comparisons, use simple tables when appropriate.
- Keep explanations student-friendly and easy to understand.
- Highlight important concepts using CAPITAL LETTERS or separators.
""",
            },
            {
                "role": "user",
                "content": user_input_to_llm
            }
        ],
        stream=True
    )

    response = ""

    for chunk in stream:
        if chunk.choices[0].delta.content:
            response += chunk.choices[0].delta.content

    print(response)
    print("\n" + "=" * 50)
    print()