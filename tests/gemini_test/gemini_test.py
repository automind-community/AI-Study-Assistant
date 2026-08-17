## gemini test file

from google import genai

study_assistant_client = genai.Client(api_key="")

# this takes te input from teh user 
usr_input_to_ai = input("Ready when you are :):")


#this records teh output froom the 
output_from_ai = study_assistant_client.models.generate_content(
    model="gemini-3.7-flash",
    contents=usr_input_to_ai
)

print(output_from_ai.text)




