## testing groq

from groq import Groq

# use yout api key here
groq_client = Groq(api_key="PASTE YOUR API KEY HERE!")
print("=================================")
print("  Welcome TO AI STUDY ASSISTANT  ")
print("=================================")
print("Type \"quit\" , \"exit\" or \"bye\" to exit")

while True:
    user_input_to_llm = input("USER:")
    if user_input_to_llm.lower() in ["quit","exit","bye"]:
        print("Study Assitant : Goodbye! Happy learning!")
        break
    print("Study Assistant: ", end="" , flush=True)
    stream = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role":"system","content":"You are a helpful Study Assistant"},
            {"role":"user","content": user_input_to_llm}
        ],
        stream=True
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content,end="", flush=True)

    print()


