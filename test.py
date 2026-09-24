import requests

API_KEY = "sk-459d3882a6280a541336e936bc2fecc733889c7264273a60"
URL = "https://keystore.edumai.tech/v1"

payload = {
    "model": "key/deepseek-v4-flash",
    "messages": [
        {
            "role": "user",
            "content": "Jelaskan apa itu Flask dalam 3 kalimat."
        }
    ],
    "temperature": 0.2
}

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

response = requests.post(
    URL,
    headers=headers,
    json=payload,
    timeout=120
)

response.raise_for_status()

data = response.json()

print(data["choices"][0]["message"]["content"])