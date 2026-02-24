import asyncio
import csv
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Configuration
MCP_SERVER_COMMAND = "/Users/shubby/Documents/mcp_servers/mcpenv/bin/python3"
MCP_SERVER_ARGS = ["-m", "apple_mail_mcp.server"]
OUTPUT_CSV = "data/recent_emails.csv"
LOOKBACK_HOURS = 24

def parse_date(date_str):
    """Parses an email date string into a datetime object."""
    try:
        # parsedate_to_datetime handles many standard RFC 2822 date formats
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
             # Assume local time if no timezone is provided (unlikely for email headers but possible)
             return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt
    except Exception as e:
        print(f"Warning: Could not parse date '{date_str}': {e}")
        return None

async def main():
    print(f"Connecting to Apple Mail MCP server...")
    
    server_params = StdioServerParameters(
        command=MCP_SERVER_COMMAND,
        args=MCP_SERVER_ARGS,
        env=None
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Fetch a batch of emails. 
            # We ask for a larger limit to ensure we cover the last 24 hours even if there's high volume.
            # 'limit' is technically optional in the schema but good to specify.
            print("Fetching emails...")
            try:
                # Using 'get_emails' tool. 
                # Schema: account (opt), mailbox (opt), filter (opt), limit (int, default 50)
                # We'll use filter='all' (default) and a higher limit.
                result = await session.call_tool(
                    "get_emails",
                    arguments={
                        "limit": 100 
                    }
                )
            except Exception as e:
                print(f"Error calling tool 'get_emails': {e}")
                return

            if not result.content:
                print("No content returned from tool.")
                return

            emails_data = result.content[0].text
            
            # The tool likely returns a JSON string or a list of dictionaries. 
            # Based on typical MCP implementation, text content is usually a JSON string representation.
            # However, let's verify if we need to json.loads it or if it's already structured.
            # MCP Python SDK `call_tool` returns `CallToolResult`, containing `content` list.
            # `content` items are usually `TextContent` or `ImageContent`.
            # If it's `TextContent`, `.text` is the string. 
            
            import json
            try:
                emails = json.loads(emails_data)
            except json.JSONDecodeError:
                print("Error: Could not decode JSON response from tool.")
                print("Raw response snippet:", emails_data[:200])
                return

            if emails:
                # print("DEBUG: First email raw data:")
                # print(json.dumps(emails[0], indent=2))
                pass
            
            print(f"Retrieved {len(emails)} emails. Filtering for last {LOOKBACK_HOURS} hours and fetching bodies...")
            
            filtered_emails = []
            now = datetime.now().astimezone()
            cutoff_time = now - timedelta(hours=LOOKBACK_HOURS)
            
            for email in emails:
                date_str = email.get('date_received')
                if not date_str:
                    continue
                
                try:
                    # Handle ISO format with Z for UTC
                    if date_str.endswith('Z'):
                        date_str = date_str[:-1] + '+00:00'
                    email_dt = datetime.fromisoformat(date_str)
                except ValueError as e:
                    print(f"Warning: Could not parse date '{date_str}': {e}")
                    continue

                if email_dt > cutoff_time:
                    # Fetch full email details to get the body
                    email_id = email.get('id')
                    try:
                        detail_result = await session.call_tool("get_email", arguments={"message_id": email_id})
                        if detail_result.content:
                            detail_data = json.loads(detail_result.content[0].text)
                            # detail_data contains 'content' field for the body
                            body = detail_data.get('content', '')
                        else:
                            body = ""
                    except Exception as e:
                        print(f"Warning: Could not fetch details for email {email_id}: {e}")
                        body = ""

                    # Split sender into name and email
                    from email.utils import parseaddr
                    sender_name, sender_email = parseaddr(email.get('sender', ''))

                    filtered_emails.append({
                        'sender_name': sender_name,
                        'sender_email': sender_email,
                        'subject': email.get('subject', 'No Subject'),
                        'date': date_str,
                        'body': body[:5000].replace('\n', ' ') # Limit body length for CSV readability
                    })

            print(f"Found {len(filtered_emails)} emails in the last {LOOKBACK_HOURS} hours.")
            
            if filtered_emails:
                print(f"Writing to {OUTPUT_CSV}...")
                with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['date', 'sender_name', 'sender_email', 'subject', 'body']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    
                    writer.writeheader()
                    for email in filtered_emails:
                        writer.writerow(email)
                print("Done.")
            else:
                print("No recent emails to save.")

if __name__ == "__main__":
    asyncio.run(main())
