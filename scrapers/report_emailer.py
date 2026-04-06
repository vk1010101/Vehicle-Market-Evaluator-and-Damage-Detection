import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

def send_report_email(user_email, subject, body_html, pdf_path=None):
    # Load SMTP settings from environment with sensible defaults
    sender_email = os.getenv("SENDER_EMAIL", "viditkohli86@gmail.com")
    sender_password = os.getenv("SENDER_PASSWORD", "tcfveyxtntzlpble")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = user_email
    msg["Subject"] = subject

    # Attach the body (auto-detect html vs plain text)
    subtype = "html" if ("<" in body_html and ">" in body_html) else "plain"
    msg.attach(MIMEText(body_html, subtype))

    # Attach the PDF if provided and exists
    if pdf_path:
        try:
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
                part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_path))
                msg.attach(part)
        except Exception:
            # Silently skip attachment issues to not block email sending
            pass

    # Send email via SMTP (SSL)
    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(sender_email, sender_password)
        server.send_message(msg)