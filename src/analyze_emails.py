import IPython.core.display_functions
from tqdm import tqdm
import csv
import requests
import json
import os
import sys

# Configuration
INPUT_CSV = "recent_emails.csv"
OUTPUT_CSV = "recent_emails.csv" # Overwriting the same file as requested
PROMPT_FILE = "prompt.txt"
LLM_ENDPOINT = "http://localhost:10101/v1/chat/completions"
MODEL_NAME = "google/gemma-3-4b" # LM Studio usually ignores this or uses the loaded model

def load_prompt(prompt_file):
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: Prompt file '{prompt_file}' not found.")
        sys.exit(1)

def call_llm(prompt_template, sender_name, sender_email, subject):
    prompt = prompt_template.format(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject
    )

    user_prompt = """<input_data>
            Sender Name: {sender_name}
            Sender Email: {sender_email}
            Subject: {subject}
            </input_data>

            ```bool""".format(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject
    )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0,
        "max_tokens": 50,
        "stream": False
    }

    try:
        response = requests.post(
            LLM_ENDPOINT, 
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=30
        )
        if response.status_code != 200:
             print(f"Error Code: {response.status_code}")
             print(f"Response Body: {response.text}")
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content'].strip()
    except requests.exceptions.RequestException as e:
        print(f"Error calling LLM: {e}")
        return "Error: LLM Call Failed"
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Error parsing LLM response: {e}")
        return "Error: Response Parsing Failed"

def main():
    if not os.path.exists(INPUT_CSV):
        print(f"Error: Input CSV '{INPUT_CSV}' not found.")
        return

    prompt_template = load_prompt(PROMPT_FILE)
    
    emails = []
    headers = []
    
    # Read existing emails
    with open(INPUT_CSV, 'r', newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        headers = reader.fieldnames
        if 'is_job_application' not in headers:
            headers.append('is_job_application')
        
        for row in reader:
            emails.append(row)
    
    print(f"Loaded {len(emails)} emails. Starting analysis...")

    # Process emails
    updated_count = 0
    for email in tqdm(emails, desc="Analyzing Emails", unit="email"):
        # Skip if already successfully analyzed
        existing_analysis = email.get('is_job_application')
        if existing_analysis and not existing_analysis.startswith("Error"):
            continue
            
        sender_name = email.get('sender_name', 'Unknown')
        sender_email = email.get('sender_email', 'Unknown')
        subject = email.get('subject', 'No Subject')
        
        # removed print analyzing...
        analysis = call_llm(prompt_template, sender_name, sender_email, subject)
        email['is_job_application'] = True if analysis == 'True' else False
        updated_count += 1
        # removed print result...

    if updated_count > 0:
        print(f"Updating {OUTPUT_CSV} with {updated_count} new analyses...")
        with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            writer.writerows(emails)
        print("Done.")
    else:
        print("No new emails to analyze.")

if __name__ == "__main__":
    main()
