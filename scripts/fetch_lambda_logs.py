import boto3
import time

logs_client = boto3.client('logs', region_name='ap-southeast-1')
log_group_name = '/aws/lambda/vieclambot-webhook'

try:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (15 * 60 * 1000)  # 15 minutes ago
    
    print(f"Filtering logs from {time.ctime(start_ms/1000.0)} to {time.ctime(now_ms/1000.0)}")
    
    response = logs_client.filter_log_events(
        logGroupName=log_group_name,
        startTime=start_ms,
        limit=100
    )
    
    events = response.get('events', [])
    print(f"Found {len(events)} events.")
    for event in events:
        msg = event['message'].strip()
        try:
            print(f"[{time.ctime(event['timestamp']/1000.0)}] {msg}")
        except UnicodeEncodeError:
            print(f"[{time.ctime(event['timestamp']/1000.0)}] {msg.encode('ascii', errors='replace').decode('ascii')}")
except Exception as e:
    print(f"Error filtering logs: {e}")
