
from tqdm import tqdm
import csv
import requests
import json
import os
import sys

# Configuration
INPUT_CSV = "recent_emails.csv"
OUTPUT_CSV = "recent_emails.csv"
PROMPT_FILE = "prompt_extract.txt"
LLM_ENDPOINT = "http://localhost:10101/v1/chat/completions"
MODEL_NAME = "google/gemma-3-4b"

def load_prompt(prompt_file):
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: Prompt file '{prompt_file}' not found.")
        sys.exit(1)

def call_llm(prompt_template, sender_name, subject, body):
    user_prompt = f"""<input_data>
            Sender Name: {sender_name}
            Subject: {subject}
            Body: {body[:2000]}  # Truncate body to avoid hitting token limits if necessary
            </input_data>
            """

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0,
        "max_tokens": 100,
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
        return None
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Error parsing LLM response: {e}")
        return None

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
        
        # Add new columns if they don't exist
        for col in ['company_name', 'job_profile']:
            if col not in headers:
                headers.append(col)
        
        for row in reader:
            emails.append(row)
    
    print(f"Loaded {len(emails)} emails. Starting extraction...")

    # Process emails
    updated_count = 0
    for email in tqdm(emails, desc="Extracting Job Details", unit="email"):
        # Only process if it is a job application and hasn't been extracted yet
        is_job_app = email.get('is_job_application', 'False') == 'True'
        has_details = email.get('company_name') and email.get('job_profile')
        
        if is_job_app and not has_details:
            sender_name = email.get('sender_name', 'Unknown')
            subject = email.get('subject', 'No Subject')
            body = email.get('body', '')
            
            json_response = call_llm(prompt_template, sender_name, subject, body)
            
            if json_response:
                try:
                    # Clean up markdown code blocks if present (just in case)
                    if json_response.startswith('```json'):
                        json_response = json_response[7:]
                    if json_response.endswith('```'):
                        json_response = json_response[:-3]
                        
                    data = json.loads(json_response)
                    email['company_name'] = data.get('company_name', 'Unknown')
                    email['job_profile'] = data.get('job_profile', 'Unknown')
                    updated_count += 1
                except json.JSONDecodeError:
                    print(f"Failed to parse JSON for email: {subject}")
                    email['company_name'] = 'Error'
                    email['job_profile'] = 'Error'
            else:
                email['company_name'] = 'Error'
                email['job_profile'] = 'Error'

    if updated_count > 0:
        print(f"Updating {OUTPUT_CSV} with {updated_count} extracted details...")
        with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            writer.writerows(emails)
        print("Done.")
    else:
        print("No new job applications to extract details from.")

if __name__ == "__main__":
    main()
