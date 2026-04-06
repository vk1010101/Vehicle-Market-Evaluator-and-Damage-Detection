import os
from dotenv import load_dotenv
from scrapers.report_emailer import send_report_email

load_dotenv()

to_email = "viditkohli@gmail.com"
subject = "Test Email from Car Scraper"
body = "Hi, it works fine."

print(f"Sending test email to {to_email}...")
try:
    send_report_email(to_email, subject, body)
    print("Email sent successfully!")
except Exception as e:
    print(f"Failed: {e}")
