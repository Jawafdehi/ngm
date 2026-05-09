import os
import requests
import sys


def notify():
    webhook_url = os.getenv("NOTIFY_WEBHOOK_URL")
    if not webhook_url:
        print("NOTIFY_WEBHOOK_URL not set, skipping notification")
        return

    job_name = os.getenv("GITHUB_JOB_NAME", "NGM Scraper")
    run_id = os.getenv("GITHUB_RUN_ID", "N/A")
    repository = os.getenv("GITHUB_REPOSITORY", "Jawafdehi/ngm")

    status = sys.argv[1] if len(sys.argv) > 1 else "FAILED"
    reason = sys.argv[2] if len(sys.argv) > 2 else "Unknown error"

    message = {
        "text": f"🚨 *{job_name}* {status} in {repository}!\nRun ID: {run_id}\nReason: {reason}"
    }

    try:
        response = requests.post(webhook_url, json=message)
        response.raise_for_status()
        print("Notification sent successfully")
    except Exception as e:
        print(f"Failed to send notification: {e}")


if __name__ == "__main__":
    notify()
